package platform

import (
	"errors"
	"os"
	"strconv"
	"syscall"
)

// DefaultUploadReserveBytes is emergency headroom, not a disk capacity plan.
// Admission also reserves a maximum-size incoming ZIP above this headroom.
const DefaultUploadReserveBytes uint64 = 256 * 1024 * 1024

type DiskSpace struct {
	AvailableBytes uint64 `json:"available_bytes"`
	TotalBytes     uint64 `json:"total_bytes"`
}
type UploadDiskStatus struct {
	DiskSpace
	ReserveBytes  uint64 `json:"reserve_bytes"`
	IncomingBytes uint64 `json:"incoming_bytes"`
	Admitting     bool   `json:"admitting"`
}

func configuredUploadReserve() (uint64, error) {
	raw := os.Getenv("QIQC_UPLOAD_RESERVE_BYTES")
	if raw == "" {
		return DefaultUploadReserveBytes, nil
	}
	value, err := strconv.ParseUint(raw, 10, 64)
	if err != nil || value == 0 {
		return 0, errors.New("QIQC_UPLOAD_RESERVE_BYTES must be a positive byte count")
	}
	return value, nil
}

func probeDiskSpace(path string) (DiskSpace, error) {
	var fs syscall.Statfs_t
	if err := syscall.Statfs(path, &fs); err != nil {
		return DiskSpace{}, err
	}
	if fs.Bsize <= 0 {
		return DiskSpace{}, errors.New("invalid filesystem block size")
	}
	block := uint64(fs.Bsize)
	if fs.Bavail > ^uint64(0)/block || fs.Blocks > ^uint64(0)/block {
		return DiskSpace{}, errors.New("filesystem capacity overflow")
	}
	return DiskSpace{AvailableBytes: fs.Bavail * block, TotalBytes: fs.Blocks * block}, nil
}

// UploadDiskCapacity exposes the same read-only measurement for M12 monitoring.
// Failure never means unlimited capacity. No files are removed by this guard.
func (s *Store) UploadDiskCapacity() (UploadDiskStatus, error) {
	probe := s.DiskProbe
	if probe == nil {
		probe = probeDiskSpace
	}
	space, err := probe(s.Root)
	reserve := s.UploadReserveBytes
	if reserve == 0 {
		reserve = DefaultUploadReserveBytes
	}
	state := UploadDiskStatus{DiskSpace: space, ReserveBytes: reserve, IncomingBytes: MaxZip}
	if err != nil {
		return state, err
	}
	state.Admitting = space.AvailableBytes >= reserve && space.AvailableBytes-reserve >= MaxZip
	return state, nil
}
func (s *Store) CheckUploadDisk() error {
	status, err := s.UploadDiskCapacity()
	if err != nil {
		return problem(503, "无法确认存储容量，暂不接收上传；未扣次数")
	}
	if !status.Admitting {
		return problem(503, "存储可用空间不足，暂不接收上传；请联系赛事组，未扣次数")
	}
	return nil
}
