# Optional audio downloader

Place a trusted, locally obtained `audiodl.exe` in this folder to enable the
`AudioFetcher` adapter. The executable is intentionally not included in the
repository. `AudioFetcher` does not connect to the UI; callers must only use it
for media they are authorized to download.
