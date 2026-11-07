package modelproxy

import "strings"

// Inspect structured inputs only. URLs appearing in ordinary prompt text or a
// local function's schema are inert text and remain allowed.
func remoteInput(value any) bool {
	switch v := value.(type) {
	case []any:
		for _, child := range v {
			if remoteInput(child) {
				return true
			}
		}
	case map[string]any:
		for key, child := range v {
			if key == "file_id" || key == "video_url" || key == "file_url" {
				return true
			}
			if key == "image_url" || key == "url" {
				switch uri := child.(type) {
				case string:
					if !strings.HasPrefix(uri, "data:") {
						return true
					}
				case map[string]any:
					if raw, ok := uri["url"].(string); !ok || !strings.HasPrefix(raw, "data:") {
						return true
					}
				default:
					return true
				}
			}
			if remoteInput(child) {
				return true
			}
		}
	}
	return false
}
