# Rights evidence

Store sealed, public-safe evidence records here. Private invoices, identity
documents, contracts, account screenshots, and voice consent files remain
outside Git. `evidence_ref` must not expose an absolute or `file://` path,
credentials, query tokens, fragments, or obvious secrets; use a safe opaque
reference and SHA-256 when evidence exists.
