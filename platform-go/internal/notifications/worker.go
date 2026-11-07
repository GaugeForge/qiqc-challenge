package notifications

import (
	"context"
	"time"
)

// Cycle is bounded and must be invoked by an explicitly configured worker.
// With the default local_only config it records intentions but never sends.
func (d *Dispatcher) Cycle(ctx context.Context) error {
	if _, err := d.Ingest(ctx, 200); err != nil {
		return err
	}
	if _, err := d.Recover(ctx); err != nil {
		return err
	}
	for i := 0; i < 32; i++ {
		worked, err := d.DispatchOne(ctx)
		if err != nil {
			return err
		}
		if !worked {
			break
		}
	}
	return nil
}
func (d *Dispatcher) Run(ctx context.Context) error {
	timer := time.NewTicker(2 * time.Second)
	defer timer.Stop()
	for {
		if err := d.Cycle(ctx); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
		}
	}
}
