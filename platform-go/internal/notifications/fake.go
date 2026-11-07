package notifications

import (
	"context"
	"sync"
)

// FakeTransport never opens a network connection. Accepted messages stay in
// memory for tests; callers must not expose these private recipient records.
type FakeTransport struct {
	mu         sync.Mutex
	accepted   map[string]Message
	attempts   int
	BeforeSend func(Message) error
}

func (f *FakeTransport) Kind() string { return Fake }
func (f *FakeTransport) Send(ctx context.Context, m Message) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.attempts++
	if f.accepted == nil {
		f.accepted = map[string]Message{}
	}
	if _, exists := f.accepted[m.Key]; exists {
		return nil
	}
	if f.BeforeSend != nil {
		if err := f.BeforeSend(m); err != nil {
			return err
		}
	}
	f.accepted[m.Key] = m
	return nil
}
func (f *FakeTransport) Accepted() []Message {
	f.mu.Lock()
	defer f.mu.Unlock()
	messages := make([]Message, 0, len(f.accepted))
	for _, message := range f.accepted {
		messages = append(messages, message)
	}
	return messages
}
func (f *FakeTransport) AttemptCount() int { f.mu.Lock(); defer f.mu.Unlock(); return f.attempts }
