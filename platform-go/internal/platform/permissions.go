package platform

import (
	"errors"
	"os"
	"syscall"
)

func sharedData() bool { return os.Getenv("QIQC_SHARED_DATA") == "1" }
func dataDirectoryMode() os.FileMode {
	if sharedData() {
		return 0770 | os.ModeSetgid
	}
	return 0700
}
func databaseMode() os.FileMode {
	if sharedData() {
		return 0660
	}
	return 0600
}
func packageMode() os.FileMode {
	if sharedData() {
		return 0640
	}
	return 0600
}
func ensureDataDirectory(path string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if err = os.MkdirAll(path, dataDirectoryMode().Perm()); err != nil {
			return err
		}
		info, err = os.Lstat(path)
	}
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("data directory link or special file refused")
	}
	if !sharedData() && info.Mode()&os.ModeSetgid != 0 && info.Mode().Perm()&0070 != 0 {
		return errors.New("shared data directory requires explicit QIQC_SHARED_DATA=1; refusing permission downgrade")
	}
	desired := dataDirectoryMode()
	if info.Mode()&(os.ModePerm|os.ModeSetgid) != desired {
		return os.Chmod(path, desired)
	}
	return nil
}
func dataFilePermissions(path string, mode os.FileMode) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("data link or special file refused")
	}
	if info.Mode().Perm() != mode {
		return os.Chmod(path, mode)
	}
	return nil
}
func prepareDatabaseFile(path string) error {
	fd, err := syscall.Open(path, syscall.O_RDWR|syscall.O_CREAT|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, uint32(databaseMode()))
	if err != nil {
		return err
	}
	syscall.Close(fd)
	if err = dataFilePermissions(path, databaseMode()); err != nil {
		return err
	}
	for _, suffix := range []string{"-wal", "-shm", "-journal"} {
		if _, err = os.Lstat(path + suffix); err == nil {
			if err = dataFilePermissions(path+suffix, databaseMode()); err != nil {
				return err
			}
		} else if !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}
