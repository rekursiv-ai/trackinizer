# Minimal transformers stub: ONLY the three symbols the public trackinizer
# package imports (`server/embedders/*.py`). transformers ships `py.typed`, but its
# `AutoModel.from_pretrained` / `AutoTokenizer.from_pretrained` resolve to
# `Unknown`/union types under the shipped types, which both checkers reject
# under `failOnWarnings`. The monorepo carries the full upstream stub; the
# export cannot ship 15 MB of it, so this pins exactly the surface used:
# `from_pretrained` returning a concrete tokenizer / `torch.nn.Module`, and the
# batch-encoding shape the pooler indexes.
#
# A `.pyi` here SHADOWS the whole `transformers` package, so any NEW import from
# transformers must be added below or it goes `Unknown` for every consumer.
from collections.abc import Iterator, Mapping
from pathlib import Path

from torch import Tensor, nn

# The tokenizer's output: a mapping of tensors that can move device.
class BatchEncoding(Mapping[str, Tensor]):
    def to(self, device: str) -> BatchEncoding: ...
    def __getitem__(self, key: str) -> Tensor: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...

# The tokenizer contract the embedder uses: call text(s) -> BatchEncoding.
# `text` is `str | list[str]` and `padding` is `bool | str` ("max_length"),
# mirroring the upstream `PreTrainedTokenizerBase.__call__`: the pooler counts a
# single string (`_HfTokenBatcher.count`) and pads a bucket to a fixed length
# (`padding="max_length"`).
class PreTrainedTokenizerBase:
    def __call__(
        self,
        text: str | list[str],
        *,
        padding: bool | str = ...,
        truncation: bool | str = ...,
        max_length: int = ...,
        return_tensors: str = ...,
    ) -> BatchEncoding: ...

class AutoTokenizer:
    @classmethod
    def from_pretrained(
        cls,
        pretrained_model_name_or_path: str | Path,
        *,
        padding_side: str = ...,
    ) -> PreTrainedTokenizerBase: ...

class AutoModel:
    # `revision` + `trust_remote_code` mirror the upstream
    # `_BaseAutoModelClass.from_pretrained` kwargs: the Jina loader pins a
    # commit and runs the repo's custom code (`jina.jina_load`).
    @classmethod
    def from_pretrained(
        cls,
        pretrained_model_name_or_path: str | Path,
        *,
        dtype: object = ...,
        revision: str = ...,
        trust_remote_code: bool = ...,
    ) -> nn.Module: ...
