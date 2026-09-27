<!--
Template of the report that Settings -> Performance downloads (the small ⤓ button).
diagnostics.report_md() fills the {{placeholders}} and drops this comment:

  {{title}}    "DeckDrop self-check" or "DeckDrop load measurement", in the page language
  {{meta}}     DeckDrop version · Deck model · date and time of the run
  {{summary}}  the verdict of the self-check, or how long the measurement took
  {{body}}     the result: a list of checks, or a table per section of the measurement
  {{footer}}   where the report comes from
-->
# {{title}}

{{meta}}

**{{summary}}**

{{body}}

---

_{{footer}}_
