package platform

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	maxPublicTraceExcerpt = 200 * 1024
	maxPublicTraceWire    = 480 * 1024
	maxPublicTraceSource  = 32 * 1024 * 1024
	maxPublicCLILine      = 16 * 1024 * 1024
)

var (
	publicBearerRE  = regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+`)
	publicQRTokenRE = regexp.MustCompile(`qr_[A-Za-z0-9_-]{20,}`)
	publicSKKeyRE   = regexp.MustCompile(`sk-[A-Za-z0-9_-]{20,}`)
	publicSecretRE  = regexp.MustCompile(`(?i)(["']?(?:[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|secret[_-]?key)|QIQC_MODEL_TOKEN|HY_API)["']?\s*[:=]\s*["']?)[^\s,"'\\}]+`)
)

type publicTraceFilter struct {
	lines     []string
	bytes     int
	truncated bool
	events    int
}

func (f *publicTraceFilter) full() bool { return f.bytes >= maxPublicTraceExcerpt }

func (f *publicTraceFilter) add(value map[string]any) {
	raw, err := json.Marshal(value)
	if err != nil {
		return
	}
	line := string(raw)
	if strings.Contains(line, "[truncated]") {
		f.truncated = true
	}
	if f.bytes+len(line)+1 > maxPublicTraceExcerpt {
		f.truncated = true
		return
	}
	f.lines = append(f.lines, line)
	f.bytes += len(line) + 1
	f.events++
}

func (f *publicTraceFilter) text() string {
	if len(f.lines) == 0 {
		return ""
	}
	return strings.Join(f.lines, "\n") + "\n"
}

func sanitizePublicTraceText(value, taskID, label string) string {
	if taskID != "" {
		value = strings.ReplaceAll(value, taskID, label)
	}
	value = publicBearerRE.ReplaceAllString(value, `${1}[REDACTED]`)
	value = publicQRTokenRE.ReplaceAllString(value, "[REDACTED]")
	value = publicSKKeyRE.ReplaceAllString(value, "[REDACTED]")
	value = publicSecretRE.ReplaceAllString(value, `${1}[REDACTED]`)
	return value
}

func boundedTraceString(value any, limit int, taskID, label string) string {
	var text string
	switch item := value.(type) {
	case nil:
		return ""
	case string:
		text = item
	default:
		raw, err := json.Marshal(item)
		if err != nil {
			text = fmt.Sprint(item)
		} else {
			text = string(raw)
		}
	}
	text = sanitizePublicTraceText(text, taskID, label)
	if len(text) > limit {
		text = text[:limit]
		for !utf8.ValidString(text) && len(text) > 0 {
			text = text[:len(text)-1]
		}
		text += "\n[truncated]"
	}
	return text
}

func publicTrimmedString(value any, limit int) string {
	text, _ := value.(string)
	if len(text) > limit {
		text = text[:limit]
		for !utf8.ValidString(text) && len(text) > 0 {
			text = text[:len(text)-1]
		}
		text += "\n[truncated]"
	}
	return text
}

func publicMessageText(value any) string {
	switch item := value.(type) {
	case string:
		var decoded any
		if json.Unmarshal([]byte(item), &decoded) == nil {
			return publicMessageText(decoded)
		}
		return item
	case map[string]any:
		if content, ok := item["content"]; ok {
			if text := publicMessageText(content); text != "" {
				return text
			}
		}
		if text, ok := item["text"].(string); ok {
			return text
		}
		if message, ok := item["message"]; ok {
			return publicMessageText(message)
		}
	case []any:
		parts := []string{}
		for _, child := range item {
			if text := publicMessageText(child); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	}
	return ""
}

func publicEventType(value map[string]any) string {
	for _, key := range []string{"type", "kind"} {
		if text, ok := value[key].(string); ok && text != "" {
			return text
		}
	}
	return ""
}

func addPublicTraceObject(filter *publicTraceFilter, object map[string]any, taskID, label string) {
	if item, ok := object["item"].(map[string]any); ok {
		itemType, _ := item["type"].(string)
		switch itemType {
		case "agent_message":
			filter.add(map[string]any{"event": "agent_message", "text": boundedTraceString(item["text"], 32*1024, taskID, label)})
		case "command_execution":
			filter.add(map[string]any{
				"event":     "command_execution",
				"command":   boundedTraceString(item["command"], 16*1024, taskID, label),
				"output":    boundedTraceString(item["aggregated_output"], 24*1024, taskID, label),
				"exit_code": item["exit_code"],
				"status":    boundedTraceString(item["status"], 256, taskID, label),
			})
		case "mcp_tool_call":
			filter.add(map[string]any{
				"event":     "mcp_tool_call",
				"server":    boundedTraceString(item["server"], 512, taskID, label),
				"tool":      boundedTraceString(item["tool"], 512, taskID, label),
				"arguments": boundedTraceString(item["arguments"], 12*1024, taskID, label),
				"result":    boundedTraceString(item["result"], 16*1024, taskID, label),
				"error":     boundedTraceString(item["error"], 8*1024, taskID, label),
				"status":    boundedTraceString(item["status"], 256, taskID, label),
			})
		case "todo_list":
			filter.add(map[string]any{"event": "todo_list", "items": boundedTraceString(item["items"], 8*1024, taskID, label)})
		case "error":
			filter.add(map[string]any{"event": "error", "message": boundedTraceString(item["message"], 16*1024, taskID, label)})
		}
		return
	}
	// DeepSeek harness envelope: top-level message is a JSON-RPC string.
	if messageRaw, ok := object["message"].(string); ok {
		var message map[string]any
		if json.Unmarshal([]byte(messageRaw), &message) == nil && message["method"] == "session.event" {
			params, _ := message["params"].(map[string]any)
			event, _ := params["event"].(map[string]any)
			data, _ := event["data"].(map[string]any)
			switch event["type"] {
			case "tool/call":
				filter.add(map[string]any{"event": "tool_call", "tool": boundedTraceString(data["name"], 512, taskID, label), "arguments": boundedTraceString(data["arguments"], 16*1024, taskID, label)})
			case "tool/result":
				filter.add(map[string]any{"event": "tool_result", "tool_call_id": boundedTraceString(data["callId"], 512, taskID, label), "output": boundedTraceString(publicMessageText(data["message"]), 24*1024, taskID, label)})
			case "assistant/message":
				filter.add(map[string]any{"event": "agent_message", "text": boundedTraceString(publicMessageText(data["message"]), 24*1024, taskID, label)})
			case "scientific/experiment":
				filter.add(map[string]any{"event": "scientific_experiment", "kind": boundedTraceString(data["kind"], 512, taskID, label), "id": boundedTraceString(data["id"], 512, taskID, label)})
			case "scientific/tool-receipt":
				filter.add(map[string]any{"event": "scientific_tool_receipt", "tool": boundedTraceString(data["tool"], 512, taskID, label), "role": boundedTraceString(data["role"], 256, taskID, label)})
			case "compaction/summary":
				filter.add(map[string]any{"event": "compaction_summary", "summary": boundedTraceString(publicMessageText(data["summary"]), 16*1024, taskID, label)})
			case "error":
				filter.add(map[string]any{"event": "error", "message": boundedTraceString(publicMessageText(data), 16*1024, taskID, label)})
			}
			return
		}
	}
	switch publicEventType(object) {
	case "thinking":
		filter.add(map[string]any{"event": "agent_thinking", "text": boundedTraceString(object["text"], 16*1024, taskID, label)})
	case "text":
		filter.add(map[string]any{"event": "agent_message", "text": boundedTraceString(object["text"], 24*1024, taskID, label)})
	case "tool_call":
		filter.add(map[string]any{"event": "tool_call", "tool": boundedTraceString(object["tool"], 512, taskID, label), "arguments": boundedTraceString(object["input"], 16*1024, taskID, label)})
	case "tool_result":
		filter.add(map[string]any{"event": "tool_result", "tool_call_id": boundedTraceString(object["callId"], 512, taskID, label), "status": boundedTraceString(object["status"], 256, taskID, label), "output": boundedTraceString(object["result"], 24*1024, taskID, label)})
	case "error":
		filter.add(map[string]any{"event": "error", "message": boundedTraceString(object["message"], 16*1024, taskID, label)})
	case "final":
		filter.add(map[string]any{"event": "final", "text": boundedTraceString(object["text"], 16*1024, taskID, label)})
	case "session", "status", "turn.started", "turn.completed", "thread.started", "item.started", "item.updated", "item.completed":
		return
	default:
		if data, ok := object["data"]; ok {
			filter.add(map[string]any{
				"event": boundedTraceString(publicEventType(object), 512, taskID, label),
				"data":  boundedTraceString(data, 16*1024, taskID, label),
			})
		}
	}
}

func filterNativeCLI(reader io.Reader, taskID, label string) (string, bool, int) {
	filter := &publicTraceFilter{}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), maxPublicCLILine)
	for scanner.Scan() {
		if filter.full() {
			filter.truncated = true
			break
		}
		var object map[string]any
		if json.Unmarshal(scanner.Bytes(), &object) != nil {
			continue
		}
		addPublicTraceObject(filter, object, taskID, label)
	}
	if scanner.Err() != nil {
		filter.truncated = true
	}
	return filter.text(), filter.truncated, filter.events
}

func publicTraceViewFromFile(path, taskID, label string) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	gz, err := gzip.NewReader(file)
	if err != nil {
		return nil, err
	}
	defer gz.Close()
	tr := tar.NewReader(io.LimitReader(gz, 100*1024*1024))
	view := map[string]any{}
	viewRaw := ""
	cliText := ""
	cliTruncated := false
	cliEvents := 0
	trajectoryText := ""
	trajectoryTruncated := false
	finalText := ""
	receipt := map[string]any{}
	platformResult := map[string]any{}
	for {
		header, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if header.Typeflag != tar.TypeReg {
			continue
		}
		switch header.Name {
		case "public-view.txt":
			if header.Size > 500000 {
				continue
			}
			raw, err := io.ReadAll(io.LimitReader(tr, 500001))
			if err != nil {
				return nil, err
			}
			if json.Unmarshal(raw, &view) != nil {
				view = map[string]any{}
			} else {
				viewRaw = string(raw)
			}
		case "agent/native-cli.jsonl":
			text, truncated, events := filterNativeCLI(tr, taskID, label)
			if events > cliEvents {
				cliText, cliTruncated, cliEvents = text, truncated, events
			}
		case "agent/native-trajectory.jsonl":
			if header.Size > maxPublicTraceSource {
				trajectoryText = "[trajectory too large for public preview]"
				trajectoryTruncated = true
				continue
			}
			raw, err := io.ReadAll(io.LimitReader(tr, maxPublicTraceSource+1))
			if err != nil {
				return nil, err
			}
			text := sanitizePublicTraceText(strings.ToValidUTF8(string(raw), "�"), taskID, label)
			if len(text) > maxPublicTraceExcerpt {
				text = text[:maxPublicTraceExcerpt]
				for !utf8.ValidString(text) && len(text) > 0 {
					text = text[:len(text)-1]
				}
				trajectoryTruncated = true
			}
			trajectoryText = text
		case "agent/final.txt":
			raw, err := io.ReadAll(io.LimitReader(tr, 65536))
			if err == nil {
				finalText = sanitizePublicTraceText(strings.ToValidUTF8(string(raw), "�"), taskID, label)
			}
		case "native-receipt.json":
			raw, err := io.ReadAll(io.LimitReader(tr, 262144))
			if err == nil {
				json.Unmarshal(raw, &receipt)
			}
		case "platform-result.json":
			raw, err := io.ReadAll(io.LimitReader(tr, 65536))
			if err == nil {
				json.Unmarshal(raw, &platformResult)
			}
		}
	}
	if len(view) == 0 {
		view = map[string]any{"mode": "scientific", "standard_harbor_rollout": true}
		if reward, ok := receipt["reward"]; ok {
			view["reward"] = reward
		}
		if runtime, ok := receipt["runtime_seconds"]; ok {
			view["runtime_seconds"] = runtime
		}
		if reason, ok := receipt["agent_stop_reason"]; ok {
			view["agent_stop_reason"] = reason
		}
		if budget, ok := receipt["agent_budget_seconds"]; ok {
			view["agent_budget_seconds"] = budget
		}
		if reward, ok := platformResult["reward"]; ok {
			view["reward"] = reward
		}
		if seconds, ok := platformResult["seconds"]; ok {
			view["runtime_seconds"] = seconds
		}
	}
	if cliText != "" {
		view["trajectory_available"] = true
		view["trajectory_excerpt"] = cliText
		view["trajectory_truncated"] = cliTruncated || len(cliText) >= maxPublicTraceExcerpt
		view["summary"] = "Filtered Agent events recovered from the retained native CLI log. Command text and truncated stdout/stderr are included; internal task identifiers are redacted."
		view["trace_source"] = "native-cli-filtered"
	} else if trajectoryText != "" {
		view["trajectory_available"] = true
		view["trajectory_excerpt"] = trajectoryText
		view["trajectory_truncated"] = trajectoryTruncated
		view["trace_source"] = "native-trajectory-filtered"
	} else {
		existing, _ := view["trajectory_excerpt"].(string)
		if existing != "" {
			view["trajectory_excerpt"] = sanitizePublicTraceText(existing, taskID, label)
		} else {
			lines := []string{
				"No retained Agent event stream is available for this public task.",
				"This is an evidence-retention summary, not an Agent transcript.",
			}
			if reward, ok := view["reward"]; ok {
				lines = append(lines, fmt.Sprintf("Reward: %v", reward))
			}
			if runtime, ok := view["runtime_seconds"]; ok {
				lines = append(lines, fmt.Sprintf("Runtime: %v seconds", runtime))
			}
			if reason, ok := view["agent_stop_reason"]; ok {
				lines = append(lines, fmt.Sprintf("Agent stop reason: %v", reason))
			}
			if code, ok := view["evidence_failure_code"]; ok {
				lines = append(lines, fmt.Sprintf("Evidence failure code: %v", code))
			}
			if text := strings.TrimSpace(finalText); text != "" {
				lines = append(lines, "Final Agent message:", text)
			}
			view["trajectory_available"] = false
			view["trajectory_excerpt"] = sanitizePublicTraceText(strings.Join(lines, "\n"), taskID, label)
			view["trajectory_truncated"] = false
			view["trace_source"] = "retained_result_summary"
			if _, exists := view["summary"]; !exists {
				view["summary"] = "The retained archive does not contain an Agent event log. The trusted result remains authoritative."
			}
		}
	}
	if _, exists := view["summary"]; !exists || view["summary"] == "" {
		view["summary"] = "Bounded real scientific execution. Only this public task Agent log is shown."
	}
	if viewRaw != "" && cliText == "" && trajectoryText == "" {
		// The stored view is already the authoritative fallback (for example the
		// recovered pack-limit timeline). Preserve all of its fields.
		var original map[string]any
		if json.Unmarshal([]byte(viewRaw), &original) == nil {
			if excerpt, _ := original["trajectory_excerpt"].(string); excerpt != "" {
				view = original
				view["trajectory_excerpt"] = sanitizePublicTraceText(excerpt, taskID, label)
			}
		}
	}
	return marshalPublicView(view)
}

func marshalPublicView(view map[string]any) ([]byte, error) {
	for {
		raw, err := json.MarshalIndent(view, "", "  ")
		if err != nil {
			return nil, err
		}
		raw = append(raw, '\n')
		if len(raw) <= maxPublicTraceWire {
			return raw, nil
		}
		excerpt, _ := view["trajectory_excerpt"].(string)
		if excerpt == "" {
			return nil, errors.New("public trace exceeds transport limit")
		}
		cut := len(excerpt) / 2
		for cut > 0 && !utf8.ValidString(excerpt[:cut]) {
			cut--
		}
		view["trajectory_excerpt"] = excerpt[:cut]
		view["trajectory_truncated"] = true
	}
}
