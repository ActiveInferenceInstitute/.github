# The readiness model

The unit of assessment is a repository working copy and its declared readiness tier. The tier selects criteria from a versioned YAML catalogue. The checker uses a fixed vocabulary: file existence, file content, YAML paths, bounded commands, and opt-in GitHub API reads. A new criterion selects one of those kinds and supplies the applicable paths, patterns, or command rather than adding another implementation branch [@aii-readiness-catalogue].

The outcomes are pass, fail, waived, unavailable, and not applicable. An exclusion applies by tier. A waiver applies to an individual measured failure and records why it may be accepted. A missing or unusable measurement remains unavailable, including when Git identifies a root but fails to provide its tracked-file index. The gate uses separate exit codes for failing criteria, usage or configuration errors, and unavailable evidence [@aii-readiness-checker].
