# Releases

Every email the Coordinator writes waits in Replies until the HR officer releases it.
Released emails are always archived in `data/outbox/`. When live SMTP delivery is
enabled (including optional Demo Recipient Override), the released email is also sent
via SMTP and tracked on the draft (`sent`, `outbox_only`, or `failed`).
