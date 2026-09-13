# Upstream Open-PR Bug-Fix Import Review

Reviewed 2026-09-13 against `peteromallet/desloppify` while this fork's `main`
matched upstream's base commit. The upstream repository had 90 open PRs.

## Decision boundary

Import scanner and command-line bug fixes with focused regression coverage. Defer
persisted state, triage lifecycle, configuration-interface, and language-support
changes until the mending design establishes their contracts. This includes PR
[#671](https://github.com/peteromallet/desloppify/pull/671): resolution
attestations must not be reattributed in this import.

## Imported

Merged the reviewed integration PR [#744](https://github.com/peteromallet/desloppify/pull/744),
which carries these 27 fixes:

- #673, #676, #677, #680, #682, #685, #687–#692, #695–#697, #699, #703,
  #713, #729–#733, #737, #738, #741, and #743.

Merged these independent, compatible bug-fix PRs in full:

- #611, #616, #617, #629, #631, #634, #635, #653, #654, #661–#663, #666,
  #668, #670, #684, #693, #710, #735, and #739.

Imported only the independently bounded Rust coverage fixes from #712 and #722:
`include!` ownership modelling and bounded dependency resolution. The remaining
commits in those PRs are coupled to broader triage and policy behavior.

Where upstream changes exposed a defect, this branch adds or strengthens a
regression: Java PMD now asserts GNU-parseable `text` output; Kotlin/JVM source
root resolution includes `src/main/kotlin`; and C# control-flow analysis does not
mark code after a hoisted local function as unreachable.

## Deferred

Persisted-state, queue, triage, and lifecycle changes are out of scope for this
bug-fix import: #575, #636–#639, #644–#650, #652, #655–#659, #667, #671,
#686, and #694. PR #721 is mixed: its TypeScript scanner corrections are already
covered by #713 and #629; its plan-completion and queue lifecycle changes remain
deferred.

The following are duplicates, obsolete against imported code, or add a coupled
behavior change that is not independently safe to transplant:

- #620, #621, #660, #669, and #672.

The following need separate design or compatibility work before adoption:

- #614 and #734 (new language support), #622 (raw suppression matching can hide
  findings), #623 and #626 (new public
  configuration), #624 (can resolve imports outside the scan root), #630
  (golangci-lint v2 command breaks v1 installations), #632 and #633 (can
  over-credit coverage), #723 (tool-version and language-flag assumptions), and
  #740 (broad Kotlin import suppression).

The lists above account for all 90 open PRs: #744 is the integration wrapper,
49 individual PRs contributed imported code (two partially), and 40 are deferred.
