# placement-letter

Starts when: the acceptance email is drafted
Done by: plain code (the model never writes the letter)

## What it does
- Makes the placement letter as a plain A4 PDF: date, reference, the candidate's name and email,
  the letter text, a small table (Work ID, Area, Start date, End date), "Yours sincerely," and the
  department name. The basic version has no signature line.
- The wording comes from employees/onboarding/templates/placement-letter.md. Change the wording
  there; no code needs to change. The department name is in shared/letter/letter.json.
- The dates are set automatically: the placement starts on the Monday after the interview and
  ends six months later (for example 30 November 2026 to 29 May 2027). An interview on a Monday
  starts the following Monday.
- The HR officer can change the dates on the candidate page. Clearing both goes back to the
  automatic dates. Saving the dates makes the letter again. It is made once more just before release, so the
  released letter always has the latest details.
- On release, the acceptance email is saved to data/outbox as an .eml file with the letter
  attached. Nothing is sent by the system.

## Where the idea comes from
The approach follows faizanfirdousi/appointment-letter-automation (MIT license): a letter template
and an email template, with the letter PDF attached to the email. The PDF is made with the fpdf2
library (py-pdf/fpdf2).
