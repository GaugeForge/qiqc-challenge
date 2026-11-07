// Package rollout stores immutable run evidence. Submission packages never enter it.
package rollout

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
)

type Info struct {
	Key      string `json:"key"`
	SHA256   string `json:"sha256"`
	Size     int64  `json:"size"`
	Provider string `json:"provider"`
}
type S3 interface {
	PutObject(context.Context, *s3.PutObjectInput, ...func(*s3.Options)) (*s3.PutObjectOutput, error)
	HeadObject(context.Context, *s3.HeadObjectInput, ...func(*s3.Options)) (*s3.HeadObjectOutput, error)
	GetObject(context.Context, *s3.GetObjectInput, ...func(*s3.Options)) (*s3.GetObjectOutput, error)
}
type Store struct {
	Root   string
	Client S3
	Bucket string
}

func (s *Store) Provider() string {
	if s.Client != nil {
		return "cloudflare-r2"
	}
	return "local-private-substitute"
}
func ValidateKey(key string) error {
	if key == "" || strings.HasPrefix(key, "/") || strings.ContainsAny(key, "\\\x00") || path.Clean(key) != key || key == ".." || strings.HasPrefix(key, "../") {
		return errors.New("unsafe rollout key")
	}
	return nil
}
func Digest(file string) (string, int64, error) {
	f, err := os.Open(file)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)), n, err
}
func (s *Store) PutFile(ctx context.Context, key, source string) (Info, error) {
	info := Info{Key: key, Provider: s.Provider()}
	if err := ValidateKey(key); err != nil {
		return info, err
	}
	sha, size, err := Digest(source)
	if err != nil {
		return info, err
	}
	info.SHA256 = sha
	info.Size = size
	if s.Client == nil {
		dest := filepath.Join(s.Root, filepath.FromSlash(key))
		if err = sharedObjectDirectories(s.Root, filepath.Dir(dest)); err != nil {
			return info, err
		}
		if _, err = os.Stat(dest); err == nil {
			have, n, e := Digest(dest)
			if e != nil {
				return info, e
			}
			if have != sha || n != size {
				return info, errors.New("immutable rollout collision")
			}
			return info, nil
		}
		in, err := os.Open(source)
		if err != nil {
			return info, err
		}
		defer in.Close()
		out, err := os.CreateTemp(filepath.Dir(dest), ".upload-")
		if err != nil {
			return info, err
		}
		name := out.Name()
		defer os.Remove(name)
		_, err = io.Copy(out, in)
		if err == nil {
			err = out.Sync()
		}
		closeErr := out.Close()
		if err != nil {
			return info, err
		}
		if closeErr != nil {
			return info, closeErr
		}
		if os.Getenv("QIQC_SHARED_DATA") == "1" {
			if err = os.Chmod(name, 0640); err != nil {
				return info, err
			}
		}
		if err = os.Link(name, dest); err != nil && !os.IsExist(err) {
			return info, err
		}
		have, n, err := Digest(dest)
		if err != nil {
			return info, err
		}
		if have != sha || n != size {
			return info, errors.New("concurrent immutable rollout collision")
		}
		dir, err := os.Open(filepath.Dir(dest))
		if err != nil {
			return info, err
		}
		err = dir.Sync()
		dir.Close()
		return info, err
	}
	file, err := os.Open(source)
	if err != nil {
		return info, err
	}
	defer file.Close()
	// Conditional creation is atomic. A HEAD followed by unconditional PUT would
	// permit two writers to replace evidence with different bytes.
	_, err = s.Client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(s.Bucket), Key: aws.String(key), Body: file, ContentLength: aws.Int64(size), ContentType: aws.String("application/gzip"), IfNoneMatch: aws.String("*"), Metadata: map[string]string{"sha256": sha}})
	if err != nil {
		var api smithy.APIError
		if !errors.As(err, &api) || (api.ErrorCode() != "PreconditionFailed" && api.ErrorCode() != "412") {
			return info, err
		}
	}
	head, err := s.Client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(s.Bucket), Key: aws.String(key)})
	if err != nil {
		return info, err
	}
	if aws.ToInt64(head.ContentLength) != size || head.Metadata["sha256"] != sha {
		return info, errors.New("R2 immutable collision or integrity mismatch")
	}
	return info, nil
}

// WithFile verifies bytes before parsing and removes remote temporary files on
// every exit path. It does not turn VPS storage into a rollout archive.
func (s *Store) WithFile(ctx context.Context, key, expectedSHA string, fn func(string) error) error {
	if err := ValidateKey(key); err != nil {
		return err
	}
	if s.Client == nil {
		file := filepath.Join(s.Root, filepath.FromSlash(key))
		hash, _, err := Digest(file)
		if err != nil {
			return err
		}
		if hash != expectedSHA {
			return errors.New("rollout integrity mismatch")
		}
		return fn(file)
	}
	obj, err := s.Client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(s.Bucket), Key: aws.String(key)})
	if err != nil {
		return err
	}
	defer obj.Body.Close()
	if aws.ToInt64(obj.ContentLength) > 512*1024*1024 {
		return errors.New("rollout exceeds local read limit")
	}
	tmp, err := os.CreateTemp("", "qiqc-rollout-*.tar.gz")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	n, err := io.Copy(tmp, io.LimitReader(obj.Body, 512*1024*1024+1))
	closeErr := tmp.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if n > 512*1024*1024 {
		return errors.New("rollout exceeds local read limit")
	}
	hash, _, err := Digest(name)
	if err != nil {
		return err
	}
	if hash != expectedSHA {
		return errors.New("rollout integrity mismatch")
	}
	return fn(name)
}
func envFile(file string) map[string]string {
	out := map[string]string{}
	b, err := os.ReadFile(file)
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(strings.TrimPrefix(line, "export "), "=")
		if ok {
			out[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), "\"'")
		}
	}
	return out
}
func FromEnvironment(root string) (*Store, error) {
	s := &Store{Root: root}
	if os.Getenv("QIQC_R2_ENABLED") != "1" {
		return s, nil
	}
	file := os.Getenv("QIQC_R2_ENV")
	if file == "" {
		return nil, errors.New("explicit private R2 config path required")
	}
	m := envFile(file)
	for _, key := range []string{"R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"} {
		if v := os.Getenv(key); v != "" {
			m[key] = v
		}
		if m[key] == "" {
			return nil, errors.New("R2 enabled with incomplete configuration")
		}
	}
	if !strings.HasPrefix(m["R2_ENDPOINT"], "https://") {
		return nil, errors.New("R2 endpoint must use HTTPS")
	}
	s.Bucket = m["R2_BUCKET"]
	s.Client = s3.NewFromConfig(aws.Config{Region: "auto", Credentials: credentials.NewStaticCredentialsProvider(m["R2_ACCESS_KEY_ID"], m["R2_SECRET_ACCESS_KEY"], ""), HTTPClient: &http.Client{Timeout: 60 * time.Second}}, func(o *s3.Options) {
		o.BaseEndpoint = aws.String(m["R2_ENDPOINT"])
		o.UsePathStyle = true
		o.RequestChecksumCalculation = aws.RequestChecksumCalculationWhenRequired
		o.ResponseChecksumValidation = aws.ResponseChecksumValidationWhenRequired
	})
	return s, nil
}

// Shared local substitute access is opt-in for trusted API/worker identities.
// Real R2 uses separately scoped credentials and does not use these paths.
func sharedObjectDirectories(root, destination string) error {
	mode := os.FileMode(0700)
	if os.Getenv("QIQC_SHARED_DATA") == "1" {
		mode = 0770 | os.ModeSetgid
	}
	absoluteRoot, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	absoluteDest, err := filepath.Abs(destination)
	if err != nil {
		return err
	}
	rel, err := filepath.Rel(absoluteRoot, absoluteDest)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return errors.New("object directory outside store")
	}
	paths := []string{absoluteRoot}
	current := absoluteRoot
	if rel != "." {
		for _, part := range strings.Split(rel, string(os.PathSeparator)) {
			current = filepath.Join(current, part)
			paths = append(paths, current)
		}
	}
	for _, path := range paths {
		info, e := os.Lstat(path)
		if os.IsNotExist(e) {
			if e = os.MkdirAll(path, mode.Perm()); e != nil {
				return e
			}
			info, e = os.Lstat(path)
		}
		if e != nil {
			return e
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return errors.New("object directory link refused")
		}
		if info.Mode()&(os.ModePerm|os.ModeSetgid) != mode {
			if e = os.Chmod(path, mode); e != nil {
				return e
			}
		}
	}
	return nil
}
