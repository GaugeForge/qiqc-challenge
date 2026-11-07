package platform

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// SocketScanner delegates only a quarantine basename to the fixed trusted
// helper. The frontend identity never receives the Docker control socket.
func SocketScanner(socketPath string) func(string) (Report, error) {
	return func(path string) (Report, error) {
		var report Report
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return report, errors.New("invalid scanner source")
		}
		name := filepath.Base(path)
		if !strings.HasPrefix(name, "incoming-") || !strings.HasSuffix(name, ".zip") {
			return report, errors.New("invalid quarantine name")
		}
		original, err := os.ReadFile(path)
		if err != nil {
			return report, err
		}
		if len(original) > MaxZip {
			return report, problem(413, "压缩包过大")
		}
		conn, err := net.DialTimeout("unix", socketPath, 3*time.Second)
		if err != nil {
			return report, problem(503, "隔离扫描服务不可用；未扣次数")
		}
		defer conn.Close()
		if err = conn.SetDeadline(time.Now().Add(45 * time.Second)); err != nil {
			return report, err
		}
		if err = json.NewEncoder(conn).Encode(map[string]string{"filename": name}); err != nil {
			return report, err
		}
		raw, err := io.ReadAll(io.LimitReader(conn, 512*1024+1))
		if err != nil || len(raw) > 512*1024 {
			return report, problem(503, "隔离扫描报告不可用")
		}
		var response struct {
			OK        bool   `json:"ok"`
			Report    Report `json:"report"`
			Error     string `json:"error"`
			ErrorKind string `json:"error_kind"`
			SHA256    string `json:"source_sha256"`
		}
		if json.Unmarshal(raw, &response) != nil {
			return report, problem(503, "隔离扫描报告无效")
		}
		if !response.OK {
			if response.ErrorKind != "invalid_package" {
				return report, problem(503, "隔离扫描服务暂不可用；未扣次数")
			}
			return report, problem(400, publicScannerError(response.Error))
		}
		if response.SHA256 != shaBytes(original) {
			return report, problem(503, "审查字节与上传字节不一致")
		}
		if !validScannerReport(response.Report) {
			return report, problem(503, "隔离扫描报告版本无效")
		}
		return response.Report, nil
	}
}
