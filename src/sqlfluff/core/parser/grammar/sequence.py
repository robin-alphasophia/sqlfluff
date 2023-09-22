"""Sequence and Bracketed Grammars."""

# NOTE: We rename the typing.Sequence here so it doesn't collide
# with the grammar class that we're defining.
from os import getenv
from typing import Optional
from typing import Sequence as SequenceType
from typing import Set, Tuple, Type, Union

from sqlfluff.core.parser.context import ParseContext
from sqlfluff.core.parser.grammar.base import (
    BaseGrammar,
    cached_method_for_parse_context,
)
from sqlfluff.core.parser.grammar.conditional import Conditional
from sqlfluff.core.parser.match_algorithms import (
    next_ex_bracket_match2,
    resolve_bracket2,
    skip_start_index_forward_to_code,
    skip_stop_index_backward_to_code,
    trim_to_terminator2,
)
from sqlfluff.core.parser.match_result import MatchResult2
from sqlfluff.core.parser.segments import (
    BaseSegment,
    BracketedSegment,
    Dedent,
    Indent,
    MetaSegment,
    UnparsableSegment,
)
from sqlfluff.core.parser.types import MatchableType, ParseMode, SimpleHintType


def _flush_metas(
    pre_nc_idx: int, post_nc_idx: int, meta_buffer: SequenceType[Type[MetaSegment]]
) -> Tuple[Tuple[int, Type[MetaSegment]], ...]:
    """Using the balance of any metas in the buffer, determine positions.

    If they're _all positive_, then they come before any whitespace,
    but otherwise they come after. We always preserve the order.
    """
    # Flush any metas...
    if all(m.indent_val >= 0 for m in meta_buffer):
        meta_idx = pre_nc_idx
    else:
        meta_idx = post_nc_idx
    return tuple((meta_idx, meta) for meta in meta_buffer)


class Sequence(BaseGrammar):
    """Match a specific sequence of elements."""

    supported_parse_modes = {
        ParseMode.STRICT,
        ParseMode.GREEDY,
        ParseMode.GREEDY_ONCE_STARTED,
    }
    test_env = getenv("SQLFLUFF_TESTENV", "")

    @cached_method_for_parse_context
    def simple(
        self, parse_context: ParseContext, crumbs: Optional[Tuple[str]] = None
    ) -> SimpleHintType:
        """Does this matcher support a uppercase hash matching route?

        Sequence does provide this, as long as the *first* non-optional
        element does, *AND* and optional elements which preceded it also do.
        """
        simple_raws: Set[str] = set()
        simple_types: Set[str] = set()
        for opt in self._elements:
            simple = opt.simple(parse_context=parse_context, crumbs=crumbs)
            if not simple:
                return None
            simple_raws.update(simple[0])
            simple_types.update(simple[1])

            if not opt.is_optional():
                # We found our first non-optional element!
                return frozenset(simple_raws), frozenset(simple_types)
        # If *all* elements are optional AND simple, I guess it's also simple.
        return frozenset(simple_raws), frozenset(simple_types)

    def match2(
        self,
        segments: SequenceType["BaseSegment"],
        idx: int,
        parse_context: "ParseContext",
    ) -> MatchResult2:
        """Match a specific sequence of elements.

        When returning incomplete matches in one of the greedy parse
        modes, we don't return any new meta segments (whether from conditionals
        or otherwise). This is because we meta segments (typically indents)
        may only make sense in the context of a full sequence, as their
        corresponding pair may be later (and yet unrendered).

        Partial matches should however still return the matched (mutated)
        versions of any segments which _have_ been processed to provide
        better feedback to the user.
        """
        start_idx = idx  # Where did we start
        matched_idx = idx  # Where have we got to
        max_idx = len(segments)  # What is the limit
        insert_segments: Tuple[Tuple[int, Type[MetaSegment]], ...] = ()
        child_matches: Tuple[MatchResult2, ...] = ()
        first_match = True
        # Metas with a negative indent value come AFTER
        # the whitespace. Positive or neutral come BEFORE.
        # HOWEVER: If one is already there, we must preserve
        # the order. This forced ordering is fine if there's
        # a positive followed by a negative in the sequence,
        # but if by design a positive arrives *after* a
        # negative then we should insert it after the positive
        # instead.
        # https://github.com/sqlfluff/sqlfluff/issues/3836
        meta_buffer = []

        if self.parse_mode == ParseMode.GREEDY:
            # In the GREEDY mode, we first look ahead to find a terminator
            # before matching any code.
            max_idx = trim_to_terminator2(
                segments,
                idx,
                terminators=[*self.terminators, *parse_context.terminators],
                parse_context=parse_context,
            )

        # Iterate elements
        for elem in self._elements:
            # 1. Handle any metas or conditionals.
            # We do this first so that it's the same whether we've run
            # out of segments or not.
            # If it's a conditional, evaluate it.
            # In both cases, we don't actually add them as inserts yet
            # because their position will depend on what types we accrue.
            if isinstance(elem, Conditional):
                # A conditional grammar will only ever return insertions.
                # If it's not enabled it returns an empty match.
                # NOTE: No deeper match here, it seemed unnecessary.
                _match = elem.match2(segments, matched_idx, parse_context)
                # Rather than taking them as a match at this location, we
                # requeue them for addition later.
                for _, submatch in _match.insert_segments:
                    meta_buffer.append(submatch)
                continue
            # If it's a raw meta, just add it to our list.
            elif isinstance(elem, type) and issubclass(elem, Indent):
                meta_buffer.append(elem)
                continue

            # 2. Match Segments.
            # At this point we know there are segments left to match
            # on and that the current element isn't a meta or conditional.
            _idx = matched_idx
            # TODO: Need test cases to cover overmatching non code properly
            # especially around optional elements.
            if self.allow_gaps:
                # First, if we're allowing gaps, consume any non-code.
                # NOTE: This won't consume from the end of a sequence
                # because this happens only in the run up to matching
                # another element. This is as designed.
                _idx = skip_start_index_forward_to_code(segments, matched_idx, max_idx)

            # Have we prematurely run out of segments?
            if _idx >= max_idx:
                # If the current element is optional, carry on.
                if elem.is_optional():
                    continue
                # Otherwise we have a problem. We've already consumed
                # any metas, optionals and conditionals.
                # This is a failed match because we couldn't complete
                # the sequence.

                if (
                    # In a strict mode, running out a segments to match
                    # on means that we don't match anything.
                    self.parse_mode == ParseMode.STRICT
                    # If nothing has been matched _anyway_ then just bail out.
                    or matched_idx == start_idx
                ):
                    return MatchResult2.empty_at(idx)

                # On any of the other modes (GREEDY or GREEDY_ONCE_STARTED)
                # we've effectively already claimed the segments, we've
                # just failed to match. In which case it's unparsable.
                insert_segments += tuple((matched_idx, meta) for meta in meta_buffer)
                return MatchResult2(
                    matched_slice=slice(start_idx, matched_idx),
                    insert_segments=insert_segments,
                    child_matches=child_matches,
                ).wrap(
                    UnparsableSegment,
                    segment_kwargs={
                        "expected": (
                            f"{elem} after {segments[matched_idx - 1]}. Found nothing."
                        )
                    },
                )

            # Match the current element against the current position.
            with parse_context.deeper_match(name=f"Sequence-@{idx}") as ctx:
                # HACK: Segment slicing hack to limit
                elem_match = elem.match2(segments[:max_idx], _idx, ctx)

            # Did we fail to match? (totally or un-cleanly)
            if not elem_match:
                # If we can't match an element, we should ascertain whether it's
                # required. If so then fine, move on, but otherwise we should
                # crash out without a match. We have not matched the sequence.
                if elem.is_optional():
                    # Pass this one and move onto the next element.
                    continue

                if self.parse_mode == ParseMode.STRICT:
                    # In a strict mode, failing to match an element means that
                    # we don't match anything.
                    return MatchResult2.empty_at(idx)

                if (
                    self.parse_mode == ParseMode.GREEDY_ONCE_STARTED
                    and matched_idx == start_idx
                ):
                    # If it's only greedy once started, and we haven't matched
                    # anything yet, then we also don't match anything.
                    return MatchResult2.empty_at(idx)

                # On any of the other modes (GREEDY or GREEDY_ONCE_STARTED)
                # we've effectively already claimed the segments, we've
                # just failed to match. In which case it's unparsable.

                # Handle the simple case where we haven't even started the
                # sequence yet first:
                if matched_idx == start_idx:
                    return MatchResult2(
                        matched_slice=slice(start_idx, max_idx),
                        matched_class=UnparsableSegment,
                        segment_kwargs={
                            "expected": (
                                f"{elem} to start sequence. " f"Found {segments[_idx]}"
                            )
                        },
                    )

                # Then handle the case of a partial match.
                _start_idx = skip_start_index_forward_to_code(
                    segments, matched_idx, max_idx
                )
                return MatchResult2(
                    # NOTE: We use the already matched segments in the
                    # return value so that if any have already been
                    # matched, the user can see that. Those are not
                    # part of the unparsable section.
                    # NOTE: The unparsable section is _included_ in the span
                    # of the parent match.
                    # TODO: Make tests to assert that child matches sit within
                    # the parent!!!
                    matched_slice=slice(start_idx, max_idx),
                    insert_segments=insert_segments,
                    child_matches=child_matches
                    + (
                        MatchResult2(
                            # The unparsable section is just the remaining
                            # segments we were unable to match from the
                            # sequence.
                            matched_slice=slice(_start_idx, max_idx),
                            matched_class=UnparsableSegment,
                            segment_kwargs={
                                "expected": (
                                    f"{elem} after {segments[matched_idx-1]}. "
                                    f"Found {segments[_idx]}"
                                )
                            },
                        ),
                    ),
                )

            # Flush any metas...
            insert_segments += _flush_metas(matched_idx, _idx, meta_buffer)
            meta_buffer = []

            # Otherwise we _do_ have a match. Update the position.
            matched_idx = elem_match.matched_slice.stop
            parse_context.update_progress2(matched_idx)

            if first_match and self.parse_mode == ParseMode.GREEDY_ONCE_STARTED:
                # In the GREEDY_ONCE_STARTED mode, we first look ahead to find a
                # terminator after the first match (and only the first match).
                max_idx = trim_to_terminator2(
                    segments,
                    matched_idx,
                    terminators=[*self.terminators, *parse_context.terminators],
                    parse_context=parse_context,
                )
                first_match = False

            # How we deal with child segments depends on whether it had a matched
            # class or not.
            # If it did, then just add it as a child match and we're done. Move on.
            if elem_match.matched_class:
                child_matches += (elem_match,)
                continue
            # Otherwise, we un-nest the returned structure, adding any inserts and
            # children into the inserts and children of this sequence.
            child_matches += elem_match.child_matches
            insert_segments += elem_match.insert_segments

        # If we get to here, we've matched all of the elements (or skipped them).
        insert_segments += tuple((matched_idx, meta) for meta in meta_buffer)

        # Finally if we're in one of the greedy modes, and there's anything
        # left as unclaimed, mark it as unparsable.
        if self.parse_mode in (ParseMode.GREEDY, ParseMode.GREEDY_ONCE_STARTED):
            if max_idx > matched_idx:
                _idx = skip_start_index_forward_to_code(segments, matched_idx, max_idx)
                _stop_idx = skip_stop_index_backward_to_code(segments, max_idx, _idx)

                if _stop_idx > _idx:
                    child_matches += (
                        MatchResult2(
                            # The unparsable section is just the remaining
                            # segments we were unable to match from the
                            # sequence.
                            matched_slice=slice(_idx, _stop_idx),
                            matched_class=UnparsableSegment,
                            # TODO: We should come up with a better "expected" string
                            # than this
                            segment_kwargs={"expected": "Nothing here."},
                        ),
                    )
                    # Match up to the end.
                    matched_idx = _stop_idx

        return MatchResult2(
            matched_slice=slice(start_idx, matched_idx),
            insert_segments=insert_segments,
            child_matches=child_matches,
        )


class Bracketed(Sequence):
    """Match if a bracketed sequence, with content that matches one of the elements.

    Note that the contents of the Bracketed Expression are treated as an expected
    sequence.

    Changelog:
    - Post 0.3.2: Bracketed inherits from Sequence and anything within
      the the `Bracketed()` expression is treated as a sequence. For the
      content of the Brackets, we call the `match()` method of the sequence
      grammar.
    - Post 0.1.0: Bracketed was separate from sequence, and the content
      of the expression were treated as options (like OneOf).
    - Pre 0.1.0: Bracketed inherited from Sequence and simply added
      brackets to that sequence.
    """

    def __init__(
        self,
        *args: Union[MatchableType, str],
        bracket_type: str = "round",
        bracket_pairs_set: str = "bracket_pairs",
        start_bracket: Optional[MatchableType] = None,
        end_bracket: Optional[MatchableType] = None,
        allow_gaps: bool = True,
        optional: bool = False,
        parse_mode: ParseMode = ParseMode.STRICT,
    ) -> None:
        # Store the bracket type. NB: This is only
        # hydrated into segments at runtime.
        self.bracket_type = bracket_type
        self.bracket_pairs_set = bracket_pairs_set
        # Allow optional override for special bracket-like things
        self.start_bracket = start_bracket
        self.end_bracket = end_bracket
        super().__init__(
            *args,
            allow_gaps=allow_gaps,
            optional=optional,
            parse_mode=parse_mode,
        )

    @cached_method_for_parse_context
    def simple(
        self, parse_context: ParseContext, crumbs: Optional[Tuple[str]] = None
    ) -> SimpleHintType:
        """Does this matcher support a uppercase hash matching route?

        Bracketed does this easily, we just look for the bracket.
        """
        start_bracket, _, _ = self.get_bracket_from_dialect(parse_context)
        return start_bracket.simple(parse_context=parse_context, crumbs=crumbs)

    def get_bracket_from_dialect(
        self, parse_context: ParseContext
    ) -> Tuple[MatchableType, MatchableType, bool]:
        """Rehydrate the bracket segments in question."""
        bracket_pairs = parse_context.dialect.bracket_sets(self.bracket_pairs_set)
        for bracket_type, start_ref, end_ref, persists in bracket_pairs:
            if bracket_type == self.bracket_type:
                start_bracket = parse_context.dialect.ref(start_ref)
                end_bracket = parse_context.dialect.ref(end_ref)
                break
        else:  # pragma: no cover
            raise ValueError(
                "bracket_type {!r} not found in bracket_pairs of {!r} dialect.".format(
                    self.bracket_type, parse_context.dialect.name
                )
            )
        return start_bracket, end_bracket, persists

    def match2(
        self,
        segments: SequenceType["BaseSegment"],
        idx: int,
        parse_context: "ParseContext",
    ) -> MatchResult2:
        """Match against this matcher."""
        # Rehydrate the bracket segments in question.
        # bracket_persists controls whether we make a BracketedSegment or not.
        start_bracket, end_bracket, bracket_persists = self.get_bracket_from_dialect(
            parse_context
        )
        # Allow optional override for special bracket-like things
        start_bracket = self.start_bracket or start_bracket
        end_bracket = self.end_bracket or end_bracket

        # Are we dealing with a pre-existing BracketSegment?
        if segments[idx].is_type("bracketed"):
            # This feels a little risky to assume that the content is necessarily
            # the same. TODO: Revisit whether this is too bullish.
            return MatchResult2(matched_slice=slice(idx, idx + 1))

        # Otherwise try and match the segments directly.
        # Look for the first bracket
        with parse_context.deeper_match(name="Bracketed-Start") as ctx:
            start_match = start_bracket.match2(segments, idx, ctx)

        if not start_match:
            # Can't find the opening bracket. No Match.
            return MatchResult2.empty_at(idx)

        # NOTE: Ideally we'd match on the _content_ next, providing we were sure
        # we wouldn't hit the end. But it appears the terminator logic isn't
        # robust enough for that yet. Until then, we _first_ look for the closing
        # bracket and _then_ match on the inner content.
        bracket_match = resolve_bracket2(
            segments,
            opening_match=start_match,
            opening_matcher=start_bracket,
            start_brackets=[start_bracket],
            end_brackets=[end_bracket],
            parse_context=parse_context,
        )

        if not bracket_match:
            raise NotImplementedError(
                f"BRACKETED. WE'RE GOING TO NEED THIS. CASE 5 {bracket_match}"
            )

        # Work forward through any gaps at the start and end.
        # NOTE: We assume that all brackets are single segment.
        _idx = start_match.matched_slice.stop
        _end_idx = bracket_match.matched_slice.stop - 1
        if self.allow_gaps:
            _idx = skip_start_index_forward_to_code(segments, _idx)
            _end_idx = skip_stop_index_backward_to_code(segments, _end_idx, _idx)

        # Try and match content, clearing and adding the closing bracket
        # to the terminators.
        with parse_context.deeper_match(
            name="Bracketed", clear_terminators=True, push_terminators=[end_bracket]
        ) as ctx:
            # NOTE: This slice is a bit of a hack, but it's the only
            # reliable way so far to make sure we don't "over match" when
            # presented with a potential terminating bracket.
            # TODO: MAKE THIS BETTER. GET RID OF THE HACK.
            content_match = super().match2(segments[:_end_idx], _idx, ctx)

        # No complete match within the brackets? Stop here and return unmatched.
        if not content_match.matched_slice.stop == _end_idx:
            return MatchResult2.empty_at(idx)

        # Wherever we got to, work forward to find the closing bracket.
        # NOTE: We do this even if we didn't find a content match.
        with parse_context.deeper_match(name="Bracketed-End") as ctx:
            final_match, _ = next_ex_bracket_match2(
                segments,
                idx=content_match.matched_slice.stop,
                matchers=[end_bracket],
                parse_context=ctx,
            )

        if not final_match:
            raise NotImplementedError(
                "BRACKETED. WE'RE GOING TO NEED THIS? "
                f"CASE 4\n{content_match}\n{final_match}\n{segments}"
            )

        # Regardless of whether the inner match was successful, append it.
        # We're going to pick out the rest as unparsable shortly.
        working_match = start_match.append(content_match)

        # What's between the final match and the content. Hopefully just gap?
        intermediate_slice = slice(
            content_match.matched_slice.stop, final_match.matched_slice.start
        )
        if any(seg.is_code for seg in segments[intermediate_slice]):
            # Work out what to say for what we _were_ expecting.
            if len(content_match):
                expected = "Nothing else in bracketed expression."
            else:
                expected = str(self._elements)
            # Ok, there's something else in the gap. Add it as an UnparsableSegment.
            child_match = MatchResult2(
                intermediate_slice,
                UnparsableSegment,
                segment_kwargs={"expected": expected},
            )
            working_match = working_match.append(child_match)

        inserts = (
            (start_match.matched_slice.stop, Indent),
            (final_match.matched_slice.start, Dedent),
        )
        working_match = working_match.append(
            final_match,
            insert_segments=inserts,
        )
        if not bracket_persists:
            return working_match

        # Only wrap if `bracket_persists` is true
        return working_match.wrap(
            BracketedSegment,
            segment_kwargs={
                "start_bracket": (segments[start_match.matched_slice.start],),
                "end_bracket": (segments[final_match.matched_slice.stop - 1],),
            },
        )
