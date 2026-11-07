package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

func servePublicFile(w http.ResponseWriter, r *http.Request, path string) {
	if r.Method != "GET" && r.Method != "HEAD" {
		w.WriteHeader(405)
		return
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		http.NotFound(w, r)
		return
	}
	// The participant portal is released together with the API and can change
	// while a browser session remains open. Last-Modified-only responses allow
	// browsers and intermediary caches to reuse an old HTML/JS bundle after a
	// deployment. Every release must be read from the active deployment.
	w.Header().Set("Cache-Control", "no-store, max-age=0")
	w.Header().Set("Pragma", "no-cache")
	w.Header().Set("Expires", "0")
	http.ServeFile(w, r, path)
}

// Each root is an organizer-owned public artifact directory. Never serve the
// workspace root; reject dotfiles, symlinks and directory listings at all depths.
func publicFiles(site, web string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		root, rel := site, strings.TrimPrefix(r.URL.Path, "/")
		switch r.URL.Path {
		case "/":
			rel = "index.html"
		case "/submit", "/leaderboard":
			root, rel = web, "index.html"
		case "/admin", "/admin/", "/admin/leaderboard":
			root, rel = web, "admin.html"
		}
		if strings.HasPrefix(r.URL.Path, "/platform-assets/") {
			root, rel = web, strings.TrimPrefix(r.URL.Path, "/platform-assets/")
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			http.NotFound(w, r)
			return
		}
		path := root
		for _, part := range strings.Split(rel, "/") {
			if part == "" || strings.HasPrefix(part, ".") || strings.ContainsAny(part, "\\\x00") {
				http.NotFound(w, r)
				return
			}
			path = filepath.Join(path, part)
			info, err := os.Lstat(path)
			if err != nil || info.Mode()&os.ModeSymlink != 0 {
				http.NotFound(w, r)
				return
			}
		}
		servePublicFile(w, r, path)
	})
}
