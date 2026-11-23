"""Nuon tokenizer: a faithful port of the observable behavior of
nu-parser's lexer (``crates/nu-parser/src/lex.rs``, nushell 0.117.1).

The scanner works on codepoint indices into the document string; error
positions are codepoint offsets.

Token kinds:

- ``item``: a word. May contain almost anything: quotes open strings
  *anywhere* inside a word and swallow all bytes until they close, brackets
  nest, and a stray ``]`` is content except in list-item mode. Special bytes
  (``:`` in record keys, ``. ? !`` in cell paths) become their own
  single-character ``item`` tokens when they start a word.
- ``;``, ``|``, ``||``, ``eol``, ``comment``: structural tokens of the
  document level. ``|``-separated words form pipeline elements, ``eol`` and
  ``;`` separate pipelines, comments attach only at token boundaries.
- ``assign``/``redirect``: whole-word assignment and redirection forms.
  Nushell's pipeline syntax; nuon documents reject them (or, at the document
  level, a trailing redirection is dropped with its target as nushell does).

Lexing continues after an error so the caller can report the first one with
the whole token stream available, exactly like nushell accumulates
``parse_errors``; the first error is sticky.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._errors import NuonDecodeError

ITEM = "item"
SEMICOLON = ";"
PIPE = "|"
PIPEPIPE = "||"
EOL = "eol"
COMMENT = "comment"
ASSIGN = "assign"
REDIRECT = "redirect"

_ASCII_WS = " \t\n\r\f"

# Whole-word forms recognized at the end of an item scan (lex.rs:988-1063).
_ASSIGNMENT_OPS = frozenset({"=", "+=", "++=", "-=", "*=", "/="})
_REDIRECTIONS = frozenset({
    "out>", "o>", "out>>", "o>>", "err>", "e>", "err>>", "e>>",
    "out+err>", "err+out>", "o+e>", "e+o>",
    "out+err>>", "err+out>>", "o+e>>", "e+o>>",
})
# `o>|` and friends are an error in nushell and become a pipe token.
_REDIRECTION_PIPES = frozenset({
    "out>|", "o>|", "err>|", "e>|",
    "out+err>|", "err+out>|", "o+e>|", "e+o>|",
})


@dataclass(frozen=True)
class Token:
    kind: str
    start: int
    end: int


def _decode_error(doc: str, msg: str, pos: int) -> NuonDecodeError:
    return NuonDecodeError(msg, doc, pos)


class SpanLexer:
    """Lexes one span of the document, one token per :meth:`next_token` call.

    ``additional_ws`` bytes are whitespace between tokens (``,`` and newlines
    inside containers); ``special`` bytes stand alone as single-character
    item tokens when they begin a word. ``list_items`` mode makes a stray
    ``]`` an error, as inside a list literal. Comments are returned as
    ``comment`` tokens unless ``skip_comments``; ``special`` may vary between
    calls, which is how record parsing lexes keys (``:`` special) and values
    (no specials) from one stream.
    """

    def __init__(
        self,
        doc: str,
        start: int,
        end: int,
        *,
        additional_ws: str = "",
        skip_comments: bool = False,
        list_items: bool = False,
    ) -> None:
        self.doc = doc
        self.end = end
        self.additional_ws = additional_ws
        self.skip_comments = skip_comments
        self.list_items = list_items
        self.pos = start
        self.tokens: list[Token] = []
        self.error: NuonDecodeError | None = None
        self._is_complete = True

    def fail(self, msg: str, pos: int) -> None:
        if self.error is None:
            self.error = _decode_error(self.doc, msg, pos)

    def _push(self, token: Token) -> Token:
        self.tokens.append(token)
        return token

    def next_token(self, special: str = "") -> Token | None:
        """Return the next token, or ``None`` at the end of the span."""
        doc, end = self.doc, self.end
        i = self.pos
        while i < end:
            c = doc[i]
            if c == "|":
                if doc[i + 1 : i + 2] == "|":
                    self.pos = i + 2
                    self._is_complete = False
                    return self._push(Token(PIPEPIPE, i, i + 2))
                self.pos = i + 1
                self._is_complete = False
                return self._pipe_token(i)
            if c == ";":
                if not self._is_complete:
                    self.fail("extra tokens after an incomplete pipeline element", i)
                self.pos = i + 1
                self._is_complete = False
                return self._push(Token(SEMICOLON, i, i + 1))
            if c == "\r":
                i += 1
                continue
            if c == "\n":
                if "\n" in self.additional_ws:
                    i += 1
                    continue
                self.pos = i + 1
                return self._push(Token(EOL, i, i + 1))
            if c == "#":
                # A comment between tokens runs to the end of the line; the
                # newline itself is left for the eol arm.
                nl = doc.find("\n", i, end)
                if nl == -1:
                    self.pos = end
                    close = end
                else:
                    self.pos = nl
                    close = nl - 1 if doc[nl - 1] == "\r" else nl
                if self.skip_comments:
                    i = self.pos
                    continue
                return self._push(Token(COMMENT, i, close))
            if c == " " or c == "\t" or c in self.additional_ws:
                i += 1
                continue
            token, err = self._scan_item(i, special)
            if err is not None:
                self.fail(err.msg, err.pos)
            self.pos = token.end
            self._is_complete = True
            return self._push(token)
        self.pos = end
        return None

    def _pipe_token(self, i: int) -> Token:
        # A pipe directly after an end-of-line continues that pipeline: the
        # eol token becomes the pipe, and eol+comment pairs before it go away
        # (lex.rs:1370-1393).
        if self.tokens and self.tokens[-1].kind == EOL:
            self.tokens[-1] = Token(PIPE, i, i + 1)
            offset = 2
            while len(self.tokens) > offset:
                index = len(self.tokens) - offset
                if (
                    self.tokens[index].kind == COMMENT
                    and self.tokens[index - 1].kind == EOL
                ):
                    del self.tokens[index - 1]
                    offset += 1
                else:
                    break
            return self.tokens[-1]
        return self._push(Token(PIPE, i, i + 1))

    def _terminator(self, c: str, special: str, blocks: list[str]) -> bool:
        return (
            not blocks
            and (
                c in " \t\n\r|;"
                or c in self.additional_ws
                or c in special
            )
        )

    def _scan_item(self, tok_start: int, special: str) -> tuple[Token, NuonDecodeError | None]:
        """Scan one word from ``tok_start`` (lex_item_scan)."""
        doc, end = self.doc, self.end
        i = tok_start
        err: NuonDecodeError | None = None
        quote: str | None = None
        quote_is_interp = False
        interp_stack: list[str] = []  # expected closers inside $"..." subexpressions
        in_comment = False
        blocks: list[str] = []
        prev: str | None = None

        def fail(msg: str, pos: int) -> None:
            nonlocal err
            if err is None:
                err = _decode_error(doc, msg, pos)

        while i < end:
            c = doc[i]
            if quote is not None and not interp_stack:
                # Plain string body: only the closing quote and (for double
                # quotes) escapes matter; every other byte is content.
                if c == "\\" and quote == '"':
                    if i + 1 < end:
                        prev, i = c, i + 2
                        continue
                    break  # trailing backslash: the string cannot close
                if c == quote:
                    quote = None
                elif quote_is_interp and c == "(":
                    interp_stack.append(")")
                prev, i = c, i + 1
                continue
            if interp_stack:
                # Subexpression of an interpolated string: parens nest and
                # quotes open nested strings (interp_subexpr_step).
                expected = interp_stack[-1]
                if expected != ")":
                    if expected == '"' and c == "\\":
                        if i + 1 < end:
                            prev, i = c, i + 2
                            continue
                        break
                    if c == expected:
                        interp_stack.pop()
                elif c in "'\"`":
                    interp_stack.append(c)
                elif c == "(":
                    interp_stack.append(")")
                elif c == ")":
                    interp_stack.pop()
                prev, i = c, i + 1
                continue
            if c in "\n\r":
                # A newline ends a comment even inside one, and terminates
                # the word at bracket depth zero (lex.rs:680).
                in_comment = False
                if self._terminator(c, special, blocks):
                    break
                prev, i = c, i + 1
                continue
            if in_comment:
                if self._terminator(c, special, blocks):
                    break
                prev, i = c, i + 1
                continue
            if c == "#" and (prev is None or prev in _ASCII_WS):
                # A comment only starts at a token boundary or after ASCII
                # whitespace: `[voilà#tag]` is content (lex.rs:674-679).
                in_comment = True
                prev, i = c, i + 1
                continue
            if special and c in special and i == tok_start:
                i += 1
                break
            if c in "'\"`":
                quote = c
                quote_is_interp = c != "`" and prev == "$"
                prev, i = c, i + 1
                continue
            if c in "[{(":
                blocks.append(c)
                prev, i = c, i + 1
                continue
            if c == "]" or c == "}":
                opener = "[" if c == "]" else "{"
                if blocks and blocks[-1] == opener:
                    blocks.pop()
                elif c == "]" and not blocks and not self.list_items:
                    pass  # a stray `]` is content outside list-item mode
                else:
                    i += 1
                    fail(f"unbalanced `{c}`", i - 1)
                    break
                prev, i = c, i + 1
                continue
            if c == ")":
                if blocks and blocks[-1] == "(":
                    blocks.pop()
                else:
                    i += 1
                    fail("unbalanced `)`", i - 1)
                    break
                prev, i = c, i + 1
                continue
            if c == "r" and doc[i + 1 : i + 2] == "#":
                new_i, raw_err = _scan_raw_string(doc, i, end)
                if raw_err is not None:
                    fail(raw_err, i)
                    i = new_i
                    break
                prev, i = "#", new_i
                continue
            if c == "|" and doc[tok_start:i] in _REDIRECTIONS:
                # `o>|` keeps its pipe inside the word (lex.rs:857-860).
                prev, i = c, i + 1
                continue
            if self._terminator(c, special, blocks):
                break
            prev, i = c, i + 1

        if interp_stack:
            fail(f"unclosed delimiter: expected {interp_stack[0]!r}", tok_start)
        elif quote is not None:
            fail(f"unclosed delimiter: expected {quote!r}", tok_start)
        elif blocks:
            closer = {"[": "]", "{": "}", "(": ")"}[blocks[0]]
            fail(f"unclosed delimiter: expected {closer!r}", tok_start)

        text = doc[tok_start:i]
        if text in _REDIRECTION_PIPES:
            fail("redirecting to a pipe is the same as normal piping", tok_start)
            return Token(PIPE, tok_start, i), err
        if text == "&&":
            fail("the '&&' operator is not supported in nushell", tok_start)
            return Token(PIPE, tok_start, i), err
        if text == "2>":
            fail("the '2>' redirection is not supported here", tok_start)
            return Token(REDIRECT, tok_start, i), err
        if text == "2>&1":
            fail("the '2>&1' redirection is not supported here", tok_start)
            return Token(PIPE, tok_start, i), err
        if text in _ASSIGNMENT_OPS:
            return Token(ASSIGN, tok_start, i), err
        if text in _REDIRECTIONS:
            return Token(REDIRECT, tok_start, i), err
        return Token(ITEM, tok_start, i), err


def _scan_raw_string(doc: str, start: int, end: int) -> tuple[int, str | None]:
    """Scan a raw string beginning at the ``r`` of ``r#…``.

    Returns the position after the raw string and an error message when the
    form is invalid (no quote after the hashes, or no matching terminator).
    """
    pfx = 0
    pos = start + 1
    while pos < end and doc[pos] == "#":
        pfx += 1
        pos += 1
    if pos >= end or doc[pos] != "'":
        return pos, "expected `'` to open a raw string"
    pos += 1
    while pos < end:
        if (
            doc[pos] == "#"
            and doc[pos - pfx : pos + 1] == "'" + "#" * pfx
        ):
            return pos + 1, None
        pos += 1
    return pos, "unclosed raw string"


def lex_span(
    doc: str,
    start: int,
    end: int,
    *,
    additional_ws: str = "",
    special: str = "",
    skip_comments: bool = False,
    list_items: bool = False,
) -> tuple[list[Token], NuonDecodeError | None]:
    """Lex a whole span at once.

    The returned list is the lexer's own token stream: a pipe after an
    end-of-line replaces that eol token in place, so the per-call returns of
    :meth:`SpanLexer.next_token` would miss the mutation.
    """
    lexer = SpanLexer(
        doc, start, end,
        additional_ws=additional_ws,
        skip_comments=skip_comments,
        list_items=list_items,
    )
    while lexer.next_token(special) is not None:
        pass
    return lexer.tokens, lexer.error
