package rollout

import (
	"context"
	"errors"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeS3 struct {
	data        []byte
	meta        map[string]string
	fail        bool
	conditional bool
}

func (f *fakeS3) PutObject(_ context.Context, in *s3.PutObjectInput, _ ...func(*s3.Options)) (*s3.PutObjectOutput, error) {
	if f.fail {
		return nil, errors.New("injected unavailable")
	}
	f.conditional = aws.ToString(in.IfNoneMatch) == "*"
	if f.data != nil {
		return nil, &smithy.GenericAPIError{Code: "PreconditionFailed"}
	}
	f.data, _ = io.ReadAll(in.Body)
	f.meta = in.Metadata
	return &s3.PutObjectOutput{}, nil
}
func (f *fakeS3) HeadObject(context.Context, *s3.HeadObjectInput, ...func(*s3.Options)) (*s3.HeadObjectOutput, error) {
	return &s3.HeadObjectOutput{ContentLength: aws.Int64(int64(len(f.data))), Metadata: f.meta}, nil
}
func (f *fakeS3) GetObject(context.Context, *s3.GetObjectInput, ...func(*s3.Options)) (*s3.GetObjectOutput, error) {
	return &s3.GetObjectOutput{ContentLength: aws.Int64(int64(len(f.data))), Body: io.NopCloser(strings.NewReader(string(f.data)))}, nil
}
func TestAtomicCloudCollisionRetryAndTemporaryCleanup(t *testing.T) {
	ctx := context.Background()
	f := &fakeS3{}
	s := Store{Client: f, Bucket: "fixture"}
	file := filepath.Join(t.TempDir(), "source")
	os.WriteFile(file, []byte("first"), 0600)
	info, err := s.PutFile(ctx, "run/1.tar.gz", file)
	if err != nil || !f.conditional {
		t.Fatal("missing conditional creation", err)
	}
	if _, err = s.PutFile(ctx, info.Key, file); err != nil {
		t.Fatal("idempotent retry", err)
	}
	var temp string
	if err = s.WithFile(ctx, info.Key, info.SHA256, func(p string) error { temp = p; return nil }); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(temp); !os.IsNotExist(err) {
		t.Fatal("cache not deleted")
	}
	os.WriteFile(file, []byte("different"), 0600)
	if _, err = s.PutFile(ctx, info.Key, file); err == nil {
		t.Fatal("overwrite accepted")
	}
	if string(f.data) != "first" {
		t.Fatal("object overwritten")
	}
	f.fail = true
	if _, err = s.PutFile(ctx, "new", file); err == nil {
		t.Fatal("failure suppressed")
	}
	if _, err = os.Stat(file); err != nil {
		t.Fatal("source removed on failure")
	}
}
func TestLocalKeysAndEvidenceIntegrity(t *testing.T) {
	ctx := context.Background()
	s := Store{Root: t.TempDir()}
	file := filepath.Join(t.TempDir(), "source")
	os.WriteFile(file, []byte("rollout"), 0600)
	for _, k := range []string{"../x", "/x", "a/../x", "a//b", "a\\b", ""} {
		if _, err := s.PutFile(ctx, k, file); err == nil {
			t.Fatal(k)
		}
	}
	info, err := s.PutFile(ctx, "run/1", file)
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(s.Root, "run/1"), []byte("tampered"), 0600)
	if err = s.WithFile(ctx, info.Key, info.SHA256, func(string) error { return nil }); err == nil {
		t.Fatal("bad SHA accepted")
	}
}
