# Feature research: resume and context retrieval

Updated 2026-11-05. This assessment uses the current `codex/extension-autoapply-sheets`
code as its baseline. It does not claim live browser or applicant-data validation.

| Priority | Feature | Why it helps | Scope and limits |
| --- | --- | --- | --- |
| 1 | Reviewable resume shortlist | The portal currently retrieves from Qdrant, reranks, then copies the top PDF immediately. Showing several candidates with matching terms lets the applicant catch a plausible but wrong choice before an application record is made. | Use the existing private index. Recheck a selected PDF and its digest at preparation time. Keep extracted text and private paths out of Qdrant and the shortlist API. |
| 2 | Optional local semantic embeddings | The default 512-dimension feature hash recognizes shared terms but cannot reliably connect paraphrases. Qdrant's [FastEmbed guidance](https://qdrant.tech/documentation/embeddings/fastembed/) describes local embedding generation without a hosted API. | Make model download and use explicit. Version the embedding mode and rebuild the derived index when it changes. Compare ranking on a small fictional, labeled set before making it the default. |
| 3 | Saved-context note editing | Notes can be added, searched, and deleted, but a corrected fact requires deleting and recreating the note. Editing would keep this reusable context easier to maintain. | Update the private JSON source of truth, then resync its derived Qdrant index. Continue treating retrieved notes as suggestions, never automatic form answers. |

Qdrant also supports [hybrid dense and lexical retrieval](https://qdrant.tech/documentation/search/hybrid-queries/), but it would add indexing and query work. Its [search tuning guide](https://qdrant.tech/documentation/search-tuning/hybrid-search/) recommends measuring the gain with labeled queries. For this small local resume catalog, a reviewable shortlist and measured local embeddings are clearer next steps than changing retrieval architecture now.

Baseline references: `src/jobagent/portal.py` (`PortalState.prepare`),
`src/jobagent/context_index.py` (`FeatureHasher`, `ContextIndex`), and
`src/jobagent/context_memory.py` (`ContextMemory`).
