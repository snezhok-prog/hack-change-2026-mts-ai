# Team implementation rules

Read README.md and docs/STATUS.md before working. docs/ARCHITECTURE.md defines module boundaries.
Read docs/COLLABORATION.md for team ownership and SSH mailboxes. During active team work, Codex reads the roma mailbox at start, after each verified slice, and before handoff; Claude reads codex on the same schedule. Acknowledge each new message SHA before dependent work. Never imply that stopped agents receive background notifications.
Keep Python modules needed by the submission at repository root. The organizer copies only root .py files and requirements.txt.
Never commit organizer documents, knowledge-base archives, generated indexes, real contacts, transcripts, credentials or API responses. Use synthetic test fixtures.
Use environment variables for provider access and model identifiers. Never invent product facts or report an unavailable provider as a successful consultation.
Run `python -m unittest discover -s tests -v` after behavior changes. API mocks verify contracts, not model quality.
Work in focused branches, commit verified slices, and keep task status accurate. Do not overwrite teammates' changes.
No real CRM/email transmission is required. Do not send external messages or incur API charges without authorization.
