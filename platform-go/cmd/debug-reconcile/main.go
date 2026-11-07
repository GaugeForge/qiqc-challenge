package main
import("fmt";"os";"path/filepath";"github.com/gaugeforge/qiqc-platform-go/internal/platform")
func main(){s,e:=platform.Open(filepath.Join(os.Args[1],"platform.db"));fmt.Println("open",e);if e!=nil{return};defer s.Close();fmt.Println("check",s.CheckScientificReconciliation(os.Args[2],2))}
