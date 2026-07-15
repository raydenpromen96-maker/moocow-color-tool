# Physical label evidence required

Replace every registry placeholder with one real current-lot container-label file under this directory. Each `physical_label_evidence` locator must be a portable relative `labels/...` path with `record_locator.kind=whole_file`. Freeze and re-verification read every file and bind its size, file SHA-256, and whole-file record SHA-256; templates cannot freeze.
