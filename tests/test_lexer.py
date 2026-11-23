from nuon._errors import NuonDecodeError
from nuon._lexer import (
    ASSIGN,
    COMMENT,
    EOL,
    ITEM,
    PIPE,
    PIPEPIPE,
    REDIRECT,
    SEMICOLON,
    SpanLexer,
    lex_span,
)


def kinds(doc, **kw):
    tokens, _ = lex_span(doc, 0, len(doc), **kw)
    return [t.kind for t in tokens]


def texts(doc, **kw):
    tokens, _ = lex_span(doc, 0, len(doc), **kw)
    return [doc[t.start : t.end] for t in tokens]


class TestBasics:
    def test_words_and_ws(self):
        assert texts("[1, 2]") == ["[1, 2]"]
        assert texts("a b") == ["a", "b"]
        assert kinds("a b") == [ITEM, ITEM]

    def test_commas_are_not_separators_at_top_level(self):
        assert texts("a,b") == ["a,b"]

    def test_pipes_and_semis(self):
        assert kinds("a | b || c ; d") == [ITEM, PIPE, ITEM, PIPEPIPE, ITEM, SEMICOLON, ITEM]
        assert kinds("a\nb") == [ITEM, EOL, ITEM]

    def test_eol_replaced_by_leading_pipe(self):
        # a line starting with `|` continues the previous pipeline
        assert kinds("a\n| b") == [ITEM, PIPE, ITEM]

    def test_comment_line_before_pipe_continuation(self):
        assert kinds("a\n# c\n| b") == [ITEM, COMMENT, PIPE, ITEM]

    def test_comments(self):
        assert kinds("# c") == [COMMENT]
        assert kinds("a # c\nb") == [ITEM, COMMENT, EOL, ITEM]
        # glued to a word, `#` is content
        assert texts("[a#b]") == ["[a#b]"]
        # only ASCII whitespace counts as a comment boundary
        assert texts("[voilà#tag]") == ["[voilà#tag]"]

    def test_special_bytes_split_words(self):
        # `:` splits a word anywhere except inside brackets/strings
        assert texts("a:b", special=":") == ["a", ":", "b"]
        assert texts("[a:b]", special=":") == ["[a:b]"]

    def test_stray_closer_rules(self):
        # `]` is content at code level but an error in list-item mode
        tokens, err = lex_span("a]", 0, 2)
        assert ["a]"[t.start : t.end] for t in tokens] == ["a]"]
        assert err is None
        tokens, err = lex_span("a]", 0, 2, list_items=True)
        assert err is not None and "unbalanced" in err.msg
        tokens, err = lex_span("a}", 0, 2)
        assert err is not None and "unbalanced" in err.msg
        tokens, err = lex_span("a)", 0, 2)
        assert err is not None and "unbalanced" in err.msg


class TestQuotes:
    def test_quote_swallows_terminators(self):
        assert texts("['a b']") == ["['a b']"]
        assert texts("[a'b c'd]") == ["[a'b c'd]"]

    def test_quote_opens_mid_word(self):
        assert texts("r'a' b") == ["r'a'", "b"]

    def test_last_quote_wins(self):
        assert texts("['a''b']") == ["['a''b']"]
        assert texts('["a""b"]') == ['["a""b"]']

    def test_trailing_after_close_is_error(self):
        tokens, err = lex_span("'a'\"b\"", 0, 6)
        assert err is None  # lexing itself is fine; parse_string rejects it
        assert texts("'a'\"b\"") == ["'a'\"b\""]

    def test_unclosed_string(self):
        _, err = lex_span('["abc', 0, 5)
        assert err is not None and "unclosed" in err.msg

    def test_escape_keeps_quote_open(self):
        assert texts('["a\\"b"]') == ['["a\\"b"]']
        _, err = lex_span('["\\', 0, 3)
        assert err is not None

    def test_backtick_is_a_quote(self):
        assert texts("`a b`") == ["`a b`"]

    def test_interpolated_string_extends_over_parens(self):
        assert texts('$"a (b | c) d"') == ['$"a (b | c) d"']
        assert texts("$'x (1) y'") == ["$'x (1) y'"]
        _, err = lex_span('$"a (b', 0, 5)
        assert err is not None and "unclosed" in err.msg


class TestRawStrings:
    def test_raw_string_token(self):
        assert texts("r#'a b'#") == ["r#'a b'#"]
        assert texts("r##'it'#s'##") == ["r##'it'#s'##"]
        assert texts("[r#'x'#, 1]") == ["[r#'x'#, 1]"]

    def test_raw_string_mid_word(self):
        # any `r` directly followed by `#` commits to a raw string
        tokens, err = lex_span("ar#'b'#", 0, 7)
        assert err is None
        assert texts("ar#'b'#") == ["ar#'b'#"]
        _, err = lex_span("ar#foo", 0, 6)
        assert err is not None and "`'`" in err.msg

    def test_unclosed_raw_string(self):
        _, err = lex_span("r#'abc", 0, 6)
        assert err is not None and "unclosed raw string" in err.msg

    def test_hash_counting(self):
        assert texts("r#'x'#") == ["r#'x'#"]
        _, err = lex_span("r##'x'#", 0, 7)
        assert err is not None and "unclosed" in err.msg


class TestWholeTokenForms:
    def test_assign_and_redirects(self):
        assert kinds("=") == [ASSIGN]
        assert kinds("+=") == [ASSIGN]
        assert kinds("o>") == [REDIRECT]
        assert kinds("out>>") == [REDIRECT]

    def test_pipe_like_forms_error(self):
        tokens, err = lex_span("&&", 0, 2)
        assert err is not None and "&&" in err.msg
        assert tokens[-1].kind == PIPE
        tokens, err = lex_span("o>|", 0, 3)
        assert err is not None
        assert tokens[-1].kind == PIPE

    def test_redirection_pipe_stays_in_word(self):
        tokens, err = lex_span("o>| x", 0, 5)
        assert err is not None
        assert texts("o>| x") == ["o>|", "x"]


class TestModes:
    def test_list_mode(self):
        # commas and newlines are whitespace, `;` is a token
        tokens, err = lex_span(
            "a, b\nc ; d", 0, 10, additional_ws=",\n\r", skip_comments=True
        )
        assert err is None
        assert [t.kind for t in tokens] == [ITEM, ITEM, ITEM, SEMICOLON, ITEM]

    def test_skip_comments(self):
        tokens, err = lex_span("a # c\nb", 0, 7, skip_comments=True, additional_ws="\n\r")
        assert [t.kind for t in tokens] == [ITEM, ITEM]

    def test_record_style_pulls(self):
        doc = "a: 1 b: 2"
        lexer = SpanLexer(doc, 0, 9, additional_ws=",\n\r", skip_comments=True)
        key = lexer.next_token(special=":")
        colon = lexer.next_token(special=":")
        value = lexer.next_token(special="")
        assert (doc[key.start : key.end], doc[colon.start : colon.end], doc[value.start : value.end]) == ("a", ":", "1")

    def test_value_may_contain_colon(self):
        lexer = SpanLexer("k: a:b", 0, 6, additional_ws=",\n\r", skip_comments=True)
        key = lexer.next_token(special=":")
        colon = lexer.next_token(special=":")
        value = lexer.next_token(special="")
        assert "k: a:b"[value.start : value.end] == "a:b"


class TestErrorAccumulation:
    def test_first_error_is_sticky_and_scanning_continues(self):
        tokens, err = lex_span("'a 'b' c", 0, 8)
        assert err is not None
        assert tokens  # scanning produced a stream despite the error

    def test_error_has_position(self):
        _, err = lex_span("ok 'unclosed", 0, 12)
        assert isinstance(err, NuonDecodeError)
        assert err.pos == 3
        assert err.lineno == 1
        assert err.colno == 4
