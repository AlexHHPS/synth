# Minutes, glossary and questions

What happens to a meeting's text after transcription, what is stored and who can
read it. Text goes to your configured gateway (directly or through the optional
Hermes adapter). Audio and voiceprints never leave the Mac.

## Turn-based transcript

The ASR (whisper.cpp large-v3-turbo q5_0, hash pinned) runs on the Mac with Silero
VAD. Live captions use a pinned local Qwen runtime prepared during first-run setup.

In a call the microphone is "You"; system audio is separated by voice. System echo
picked up by the microphone is removed before ASR. The transcript is organised in
speaker turns with times; older citations to segments keep working.

## Organisation glossary

The glossary records who and what is named in meetings and what each thing is.
Each term has a kind (person, organisation, product, project, system or concept)
and a short definition.

- **Added automatically.** When a meeting in a folder is processed, the model
  extracts relevant proper nouns with a kind and a definition grounded in the
  meeting. They only count if they appear verbatim in the transcript. Any folder
  member can edit, merge or delete them; a deleted term is not added again.
  Renaming a term keeps the old spelling as a "sounds like" alias.
- **Variants** that differ only in punctuation, accents or case merge on their
  own. Common words and fillers are never terms.
- **To confirm.** Only doubtful cases stay as proposals: a spelling that sounds
  like an existing term, or a name the model flagged as uncertain.
- **Scope.** Terms from a meeting stay in its folder and are visible only to its
  members, because they carry up to five transcript excerpts as examples. A
  private meeting adds nothing to the shared glossary.
- **Use.** Minutes receive each term with its kind and definition; Whisper gets up
  to 40 approved terms as a natural sentence.
- **Alias correction (on).** Each "sounds like" alias of an approved term is
  matched as a whole word or phrase, without a model. Each alias has a mode:
  - *corrects*: invented spellings are replaced by the term;
  - *suggests*: aliases that are also real Spanish words or names (Zipf ≥ 3 in
    `wordfreq`) are shown as a suggestion; one click accepts or rejects;
  - applied by context when the same meeting spells the term correctly;
  - *inactive*: shorter than three letters, equal to another term or shared by
    two terms; only helps Whisper and the minutes.
  Two acceptances without a rejection promote a suggestion to a correction; two
  rejections demote it; a single "no" returns a promoted alias to suggestion.
  Disable with `SYNTH_GLOSSARY_ALIASES=0`.
- **Model correction (off).** `SYNTH_GLOSSARY_AUTOCORRECT=1` replaces alias
  correction with a phonetic preselection that the model may only accept or
  reject. It is off by default because it made too many wrong changes.
- In both cases the raw ASR output is never modified: a correction is a new
  version with a change log, and every change can be undone where it was made.

## Minutes

Minutes are generated in phases (topics, per-topic extraction with citations,
critique, specificity gate and TL;DR). Rules enforced by code, not the model:

- A task only has an owner if that person commits or is named in the citation;
  otherwise it stays unassigned.
- A date is resolved only when the spoken expression determines it.
- A "Speaker N" is never given a name.
- A generic point is rewritten once and dropped if it is still generic.
- Open tasks and decisions from earlier meetings in the folder are reported as
  done, in progress, contradicted or not mentioned, with citations.
- Tasks and decisions are editable rows; regenerating keeps human edits.

Drafts are saved while they stream and resume after interruptions; a newer
transcript version or a changed folder context fences older generations.

## Ask your meetings

The API retrieves relevant turns only from meetings the asker can read (for an
integration, only from its key's folders). The model can only cite those
excerpts; without a valid citation the answer is "not stated". Questions and
answers are visible only to the asker, access is rechecked on every read and
they expire after 7 days. MCP exposes `ask_meetings` and `get_answer` with the
same permissions.

## Follow-up email draft

"Copy email draft" composes decisions, next steps with owner and date, and open
questions from the minutes. It is copied to the clipboard; the product has no
recipients, SMTP or any route that sends email.

## Retention

| Data | Where | How long |
| --- | --- | --- |
| Capture audio | Mac | Until acoustic analysis ends; at most 24 h while the host runs |
| Evaluation audio (opt-in) | Mac, AES-GCM | Until `python3 -m synth.eval purge` |
| Per-meeting speaker voiceprint | Mac, AES-GCM, Keychain key | 30 days, or until that speaker is confirmed, rejected, renamed or merged |
| Raw and corrected transcripts | Central database | While the meeting exists |
| Glossary terms and examples | Central database, folder scope | Until deleted or merged |
| Questions and retrieved excerpts | Central database, asker only | 7 days |
