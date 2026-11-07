package platform

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
	"unicode/utf8"
)

// Assistant answers staff questions about the console. It is read-only: it
// sees the operations manuals and the caller's current admin snapshot, and it
// cannot perform actions. Credentials come only from the service environment.
type Assistant struct {
	BaseURL   string
	APIKey    string
	Model     string
	Fallback  string
	Effort    string
	Knowledge string
	Client    *http.Client
}

// AssistantFromEnvironment returns nil when GPT_BASE_URL or GPT_API_KEY is unset.
func AssistantFromEnvironment(knowledgeFiles []string) (*Assistant, error) {
	base, key := strings.TrimSpace(os.Getenv("GPT_BASE_URL")), strings.TrimSpace(os.Getenv("GPT_API_KEY"))
	if base == "" || key == "" {
		return nil, nil
	}
	a := &Assistant{BaseURL: strings.TrimRight(base, "/"), APIKey: key, Model: env("QIQC_ASSISTANT_MODEL", "gpt-6-sol"), Fallback: env("QIQC_ASSISTANT_FALLBACK_MODEL", "gpt-5.6-sol"), Effort: env("QIQC_ASSISTANT_EFFORT", "high"), Client: &http.Client{Timeout: 240 * time.Second}}
	var b strings.Builder
	for _, path := range knowledgeFiles {
		if path = strings.TrimSpace(path); path == "" {
			continue
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("assistant knowledge %s: %w", path, err)
		}
		fmt.Fprintf(&b, "\n\n===== %s =====\n%s", baseName(path), raw)
	}
	a.Knowledge = b.String()
	return a, nil
}

func env(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func baseName(path string) string {
	if i := strings.LastIndex(path, "/"); i >= 0 {
		return path[i+1:]
	}
	return path
}

type AssistantMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

const assistantInstructions = `You are the built-in help assistant of the QIQC Challenge staff operations console (https://173.249.36.41/admin).
Staff ask you how to use the console, what a status or alert means, and what to do next. Answer in the language of the question (Chinese or English). The console has an English/中文 switch; when answering in Chinese, name controls by their Chinese labels where obvious and keep the English label in parentheses.
Be concrete: name the console tab, the field, the button, and the exact steps. Keep answers short unless asked for detail.
Ground every answer in the operations manuals and the live console snapshot below. If the snapshot does not contain the answer, say so and say where in the console to look. Never invent data, team names, IDs or numbers.
You are read-only. You cannot click, approve, retry, change quotas or send email. When an action is needed, tell the staff member exactly how to do it themselves, including the required reason.
Snapshot times are Unix seconds; present times in Asia/Singapore (UTC+8). Do not reveal secrets; the snapshot contains none.`

func (s *Store) AskAssistant(ctx context.Context, token string, messages []AssistantMessage, outline string) (string, error) {
	if s.Assistant == nil {
		return "", problem(503, "The assistant is not configured on this server.")
	}
	if len(messages) == 0 || len(messages) > 30 {
		return "", problem(400, "Send between 1 and 30 messages.")
	}
	total := 0
	for _, m := range messages {
		if (m.Role != "user" && m.Role != "assistant") || strings.TrimSpace(m.Content) == "" || !utf8.ValidString(m.Content) {
			return "", problem(400, "Invalid assistant message.")
		}
		total += len(m.Content)
	}
	if total > 45000 || messages[len(messages)-1].Role != "user" {
		return "", problem(400, "The conversation is too long. Start a new conversation.")
	}
	// adminSnapshot enforces the changed-password admin session.
	snapshot, err := s.adminSnapshot(token, snapshotOptions{pages: map[string]int{}})
	if err != nil {
		return "", err
	}
	snapshot["phase_now"], _ = s.Phase()
	live, _ := json.Marshal(snapshot)
	if len(live) > 120000 {
		live = append(live[:120000], []byte("…(truncated)")...)
	}
	if len(outline) > 16000 || !utf8.ValidString(outline) {
		outline = ""
	}
	system := assistantInstructions + "\n\n# Operations manuals" + s.Assistant.Knowledge +
		"\n\n# Current console layout (tabs and their headings, fields and buttons, as rendered for this staff member)\n" + outline + "\n\n# Live console snapshot (JSON, read at " + s.Now().UTC().Format(time.RFC3339) + ")\n" + string(live)
	all := append([]AssistantMessage{{Role: "system", Content: system}}, messages...)
	reply, err := s.Assistant.complete(ctx, s.Assistant.Model, all)
	if err != nil && s.Assistant.Fallback != "" && s.Assistant.Fallback != s.Assistant.Model && ctx.Err() == nil {
		slog.Warn("assistant primary model failed; using fallback", "model", s.Assistant.Model, "fallback", s.Assistant.Fallback, "error", err)
		reply, err = s.Assistant.complete(ctx, s.Assistant.Fallback, all)
	}
	if err != nil {
		slog.Error("assistant request failed", "error", err)
		return "", problem(502, "The assistant could not answer right now. Try again in a moment.")
	}
	return reply, nil
}

// complete calls an OpenAI-compatible Chat Completions endpoint.
func (a *Assistant) complete(ctx context.Context, model string, messages []AssistantMessage) (string, error) {
	raw, _ := json.Marshal(map[string]any{"model": model, "reasoning_effort": a.Effort, "messages": messages})
	req, err := http.NewRequestWithContext(ctx, "POST", a.BaseURL+"/chat/completions", bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+a.APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.Client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if resp.StatusCode != 200 || json.Unmarshal(body, &out) != nil || len(out.Choices) == 0 || out.Choices[0].Message.Content == "" {
		return "", fmt.Errorf("%s: status %d: %s", model, resp.StatusCode, body[:min(len(body), 300)])
	}
	return out.Choices[0].Message.Content, nil
}

func (s *Store) registerAssistant(mux *http.ServeMux) {
	mux.HandleFunc("/api/admin/assistant", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct {
			Messages []AssistantMessage `json:"messages"`
			Outline  string             `json:"ui_outline"`
		}
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "Invalid assistant message.")
		}
		_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(280 * time.Second))
		reply, err := s.AskAssistant(r.Context(), cookieToken(r), d.Messages, d.Outline)
		if err != nil {
			return err
		}
		respond(w, map[string]string{"reply": reply})
		return nil
	}))
}
