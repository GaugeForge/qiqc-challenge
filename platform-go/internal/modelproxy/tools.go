package modelproxy

// A namespace only groups local tool declarations. It does not authorize any
// provider-executed tools. Bound nesting and total declarations before forwarding.
func allowedTools(value any, allowed []string, depth int, count *int) bool {
	list, ok := value.([]any)
	if !ok || depth > 4 {
		return false
	}
	for _, entry := range list {
		*count++
		if *count > 128 {
			return false
		}
		tool, ok := entry.(map[string]any)
		if !ok {
			return false
		}
		typ, ok := tool["type"].(string)
		if !ok {
			return false
		}
		if typ == "namespace" {
			name, ok := tool["name"].(string)
			if !ok || name == "" || len(name) > 128 {
				return false
			}
			if !allowedTools(tool["tools"], allowed, depth+1, count) {
				return false
			}
			continue
		}
		permitted := false
		for _, local := range allowed {
			if typ == local {
				permitted = true
				break
			}
		}
		if !permitted {
			return false
		}
	}
	return true
}
