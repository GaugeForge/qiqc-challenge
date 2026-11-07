// Command rollout-check validates Go R2 I/O using one disposable smoke object.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"os"
	"path/filepath"
	"time"
)

func main() {
	if err := run(); err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"status": "failed", "error_type": fmt.Sprintf("%T", err)})
		os.Exit(1)
	}
}
func run() error {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	temp, err := os.MkdirTemp("", "qiqc-go-r2-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	store, err := rollout.FromEnvironment(filepath.Join(temp, "objects"))
	if err != nil {
		return err
	}
	if store.Provider() != "cloudflare-r2" {
		return fmt.Errorf("real R2 configuration required")
	}
	key := fmt.Sprintf("smoke/go-conditional-%d.tar.gz", time.Now().UnixNano())
	source := filepath.Join(temp, "source")
	if err = os.WriteFile(source, []byte("QIQC organizer-owned conditional storage smoke"), 0600); err != nil {
		return err
	}
	client := store.Client.(*s3.Client)
	defer client.DeleteObject(context.Background(), &s3.DeleteObjectInput{Bucket: aws.String(store.Bucket), Key: aws.String(key)})
	info, err := store.PutFile(ctx, key, source)
	if err != nil {
		return err
	}
	if _, err = store.PutFile(ctx, key, source); err != nil {
		return err
	}
	if err = store.WithFile(ctx, key, info.SHA256, func(string) error { return nil }); err != nil {
		return err
	}
	if err = os.WriteFile(source, []byte("different bytes must never replace original"), 0600); err != nil {
		return err
	}
	if _, err = store.PutFile(ctx, key, source); err == nil {
		return fmt.Errorf("immutable collision accepted")
	}
	if err = store.WithFile(ctx, key, info.SHA256, func(string) error { return nil }); err != nil {
		return err
	}
	if _, err = client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(store.Bucket), Key: aws.String(key)}); err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"status": "passed", "conditional_put": true, "duplicate_idempotent": true, "different_bytes_rejected": true, "readback_sha_verified": true, "temporary_object_deleted": true, "provider": "cloudflare-r2", "checked_at": time.Now().UTC().Format(time.RFC3339)})
}
