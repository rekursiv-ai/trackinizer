"""Wire contract for chat uploads: a page puts files into the assistant's workspace box.

The box takes a file straight from the page, so its bytes never pass through the
server; the server only hands the page links the assistant signed, each for one path:

- The page ``POST``s :data:`CHAT_UPLOADS_PATH` with a :class:`ChatUploadAsk`, as one
  who may post a line to the chat, and holds the request.
- The assistant holds a ``GET`` on :data:`CHAT_UPLOADS_WAIT_PATH`; an ask returns at
  once as an :class:`UploadAsk`, ``null`` when none came.
- The assistant ``POST``s an :class:`UploadAnswer` to :data:`CHAT_UPLOAD_ANSWER_PATH`,
  which ends the page's wait with a :class:`ChatUploadGrant`, or with the reason.

The page then sends each file to its link and names the box paths in its line's
``attachments`` (:class:`~trackinizer.wire.wire_science_chat.ChatSend`), which
reach the assistant in the line's context. Nothing of an upload is stored here.

The same relay carries the page's two other asks about a file it uploaded, each by its
box path and answered by the assistant the same way: a download link for it
(:data:`CHAT_FILE_LINK_PATH`, answered with a ``url``), and its removal, as when an
upload is cancelled part way (:data:`CHAT_FILE_REMOVE_PATH`, answered ``removed``).

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Self

import re
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "CHAT_FILE_LINK_PATH",
    "CHAT_FILE_REMOVE_PATH",
    "CHAT_UPLOADS_PATH",
    "CHAT_UPLOADS_WAIT_PATH",
    "CHAT_UPLOAD_ANSWER_PATH",
    "MAX_UPLOAD_FILES",
    "UPLOADS_WAIT_MAX_SEC",
    "BoxPath",
    "ChatFileAsk",
    "ChatFileLink",
    "ChatUploadAsk",
    "ChatUploadFile",
    "ChatUploadGrant",
    "ChatUploadSlot",
    "UploadAnswer",
    "UploadAsk",
    "box_path",
]

CHAT_UPLOADS_PATH: Final = "/api/chats/uploads"
"""``POST``: a signed-in writer asks for one upload link per file; held until the
assistant answers."""

CHAT_UPLOADS_WAIT_PATH: Final = "/api/chats/uploads/wait"
"""``GET ?wait_sec=N``, held up to :data:`UPLOADS_WAIT_MAX_SEC`: the assistant's next
ask, or ``null`` when none came. The configured assistant's key alone."""

CHAT_UPLOAD_ANSWER_PATH: Final = "/api/chats/uploads/{request_id}"
"""``POST``: the assistant's :class:`UploadAnswer` to an ask it was handed."""

CHAT_FILE_LINK_PATH: Final = "/api/chats/files/link"
"""``POST`` a :class:`ChatFileAsk`: a download link to a file the asker uploaded, as a
:class:`ChatFileLink`; held until the assistant answers."""

CHAT_FILE_REMOVE_PATH: Final = "/api/chats/files/remove"
"""``POST`` a :class:`ChatFileAsk`: remove a file the asker uploaded; 204 once the
assistant removed it."""

MAX_UPLOAD_FILES: Final = 20
"""Files one ask, and one line's ``attachments``, may name."""

UPLOADS_WAIT_MAX_SEC: Final = 25
"""The longest the assistant's wait is held; under a client's 30 s read timeout."""

_NAME: Final = re.compile(r"[^/\\\x00-\x1f\x7f]{1,255}")


def box_path(path: str) -> str:
    """Return ``path`` if it is an absolute, normal, one-line path of at most 512 chars.

    Args:
      path: A path on the assistant's box.

    Returns:
      path: The same path.

    Raises:
      ValueError: It is relative, holds ``.``, ``..`` or an empty part, a control
        character, or is longer than 512 characters.

    """
    parts = path.split("/")
    if (
        len(path) > 512
        or not path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts[1:])
        or re.search(r"[\x00-\x1f\x7f]", path) is not None
        or str(PurePosixPath(path)) != path
    ):
        raise ValueError(f"not a box path: {path!r}")
    return path


type BoxPath = Annotated[str, AfterValidator(box_path)]
"""An absolute, normal path on the assistant's box."""


def _file_name(name: str) -> str:
    """Return ``name`` if it names a file in a folder: no separator, never . or .."""
    if _NAME.fullmatch(name) is None or name in {".", ".."}:
        raise ValueError(f"not a file name: {name!r}")
    return name


class ChatUploadFile(BaseModel):
    """One file the page is about to upload, as the person's computer names it."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, AfterValidator(_file_name)]
    """Its name, with no folder: 1 to 255 characters, no separator or control."""

    size: int = Field(ge=0)
    """Its size in bytes, as the page read it."""

    dataset: bool = False
    """Whether the person asked for it as a dataset, which the assistant keeps in a
    folder of its own where campaigns read data, unpacked once its line is sent."""


class ChatUploadAsk(BaseModel):
    """A page's ask for upload links, from the canvas a line would be posted from."""

    model_config = ConfigDict(extra="forbid")

    canvas_id: uuid.UUID
    """The poster's own canvas."""

    conversation_id: uuid.UUID | None = None
    """The chat the files go with; none uses the canvas's, or a chat not yet begun."""

    files: list[ChatUploadFile] = Field(min_length=1, max_length=MAX_UPLOAD_FILES)

    @model_validator(mode="after")
    def _names_once(self) -> Self:
        """Refuse a name asked twice: both files would get one path."""
        names = [each.name for each in self.files]
        if len(set(names)) != len(names):
            raise ValueError("each file is named once")
        return self


class UploadAsk(BaseModel):
    """An ask as the assistant is handed it: who asks, from where, and for what."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    canvas_id: uuid.UUID
    conversation_id: uuid.UUID | None
    """The chat, once it has begun; ``None`` before the canvas's first line."""

    workspace_id: uuid.UUID
    """The canvas's workspace, or the asker's default, as the chat's settings
    (``workspace_issue_id``) name it: the key of the asker's box."""

    asker: str
    """The asker's attested email."""

    asker_role: str
    """The asker's role as the server attested it: ``writer`` or ``admin``."""

    action: Literal["upload", "link", "remove"] = "upload"
    """What is asked: upload links for ``files``, or a download link for ``path``, or
    the removal of ``path``."""

    files: list[ChatUploadFile] = Field(
        default_factory=list,
        max_length=MAX_UPLOAD_FILES,
    )
    """The files an upload asks links for; none for the other actions."""

    path: BoxPath | None = None
    """The file a link or a removal is for; ``None`` for an upload."""

    @model_validator(mode="after")
    def _fits_the_action(self) -> Self:
        """Require files for an upload, and one path, with no files, for the others."""
        if self.action == "upload" and (not self.files or self.path is not None):
            raise ValueError("an upload names files and no path")
        if self.action != "upload" and (self.path is None or self.files):
            raise ValueError(f"a {self.action} names one path and no files")
        return self


class ChatFileAsk(BaseModel):
    """A page's ask about one file it uploaded: a download link, or its removal."""

    model_config = ConfigDict(extra="forbid")

    canvas_id: uuid.UUID
    """The poster's own canvas."""

    conversation_id: uuid.UUID | None = None
    """The chat the file went with; none uses the canvas's."""

    path: BoxPath
    """The file's box path, as its upload slot named it."""


class ChatFileLink(BaseModel):
    """A download link to one file the asker uploaded, signed for its path alone."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=4_096, pattern=r"^https://[^\s]+$")


class ChatUploadSlot(BaseModel):
    """Where one file goes: its box path, and the link the page sends it to."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, AfterValidator(_file_name)]
    path: BoxPath
    url: str = Field(max_length=4_096, pattern=r"^https://[^\s]+$")
    """The link, signed for ``path`` alone; the page ``POST``s the file there as the
    multipart field ``file``."""


class UploadAnswer(BaseModel):
    """The assistant's answer to an ask: what its action asked for, or why not."""

    model_config = ConfigDict(extra="forbid")

    files: list[ChatUploadSlot] | None = Field(
        default=None,
        max_length=MAX_UPLOAD_FILES,
    )
    """An upload's slots, one per file."""

    url: str | None = Field(
        default=None,
        max_length=4_096,
        pattern=r"^https://[^\s]+$",
    )
    """A link's download link."""

    removed: Literal[True] | None = None
    """A removal done."""

    error: Annotated[str, Field(min_length=1, max_length=300)] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        """Require exactly one of the slots, the link, the removal and the reason."""
        given = [self.files, self.url, self.removed, self.error]
        if sum(each is not None for each in given) != 1:
            raise ValueError("send one of files, url, removed or error")
        return self


class ChatUploadGrant(BaseModel):
    """What the page is given: a slot per file it asked for, in its order."""

    model_config = ConfigDict(extra="forbid")

    files: list[ChatUploadSlot] = Field(max_length=MAX_UPLOAD_FILES)
