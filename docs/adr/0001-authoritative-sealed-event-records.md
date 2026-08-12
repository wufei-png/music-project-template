# Use sealed records as the event application receipt

Event application uses one schema-reset contract with explicit submission IDs,
normalized envelope digests, a per-ledger advisory lock, and atomic publication.
The resulting Sealed Record is also the authoritative receipt; a second receipt
store was rejected because it would duplicate ingestion truth and complicate
recovery. Release candidates additionally require an ephemeral Release Plan
Receipt because their master and multi-file output make state drift materially
more consequential than for ordinary append-only events.
