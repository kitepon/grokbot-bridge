# Identifying downloaded image candidates

The standalone image candidate is distributed as architecture-specific Docker
archives. Verify the archive SHA256 before loading it. The fixed archive, source
revision, architecture, configuration and filesystem layers identify the shipped
artifact; a local `docker image inspect` ID by itself is not an archive checksum.

These values identify different objects:

| Value | Object |
| --- | --- |
| Archive SHA256 | The complete downloaded `.docker.tar.gz` file |
| Config digest | The image configuration JSON, referenced by the OCI manifest |
| OCI manifest digest | The manifest JSON, which references the config and layers |
| Registry digest | A manifest or index published by a registry; this archive release does not publish one |
| Local inspect `.Id` | The image ID observed on a particular Docker Engine/image store |

The original `candidate-identity.json` records `image_id` as observed on the native
build runners. Those values equal the config digests in these archives. During
distribution verification, a Docker Engine using the containerd image store
reported the archives' OCI manifest digests as local inspect IDs instead. Both
observations were checked against the same downloaded bytes and their config,
source, architecture, RootFS and startup settings. This is an observation of the
verified environments, not a promise that every Engine reports the same ID.

For candidate source `aab624eaf8f336ec1061114e3316172fac6a1563`:

| Architecture | Config digest / original build image ID | Archive OCI manifest digest / observed containerd inspect ID |
| --- | --- | --- |
| linux/amd64 | `sha256:a2af5934a432742e3ddadae2ccbdff7036079abc6d6095466e575b0821980bc8` | `sha256:6696d79686ee36bf4555016e0b47e37144c506fa9a8eb0f44ea53f5993faac4b` |
| linux/arm64 | `sha256:169c3402c67209be98d0782612f06963e74155fadafbf2865fdf0f041c2865ef` | `sha256:c57c903342f8b3c7ea63fc7f01bc3356b218635497b4fe3047ad87a77b0cbdc1` |

The release's additive
[image-identity-clarification.json](https://github.com/kitepon/grokbot-bridge/releases/download/candidate-20261023-aab624eaf8f3/image-identity-clarification.json)
records these fields explicitly. The original archives, checksums, candidate tag,
source and `candidate-identity.json` remain unchanged. There is no need to rebuild
an image because these local ID observations differ.

For an archive, `manifest.json` identifies the image's Config file, and
`index.json` points to the OCI manifest under `blobs/sha256/`. Hash the actual
referenced JSON bytes to compare their digests. Check that the OCI manifest's
config digest points to the expected config, and compare the source label,
architecture, RootFS and startup settings with the release metadata. A different
local inspect ID alone does not prove a different artifact, and an ID matching one
of the recorded values alone does not replace the archive/source checks.

Use the release's loaded image name when selecting this candidate in Compose;
the OCI manifest digest stored inside a downloaded archive is not a newly
published registry pull reference.

References: [OCI image manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md),
[OCI image configuration](https://github.com/opencontainers/image-spec/blob/main/config.md),
[Docker containerd image store](https://docs.docker.com/engine/storage/containerd/).
