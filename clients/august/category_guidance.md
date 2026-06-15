# How August categorizes expenses (guidance for the categorization model)

August's chart of accounts is INTENT-based, not merchant-based. The same
merchant lands in different categories depending on *why* the money was spent.
A coffee or meal could be any of: a team ritual, a client meeting, conference
spend, or just groceries. When the intent isn't knowable from the merchant
name alone, make your best guess and keep confidence low — a human reviews it.

## Disambiguation that matters most here

- **Travel (airlines, hotels, rideshare, airport food)** is usually either
  `General Travel`, `Billable Expense` (travel for a specific client engagement),
  or `Sales, Speaking & Conferences` (travel to speak/attend a conference).
  Default to `General Travel` unless there's a clear client or event signal.
- **Meals / coffee / restaurants**: `Groceries & Meals` is the safe default, but
  `Team Culture:Friday Lunch` (recurring team lunch), `Billable Expense` (client
  meal), and `Sales - Client Engagement & Lead Development` (prospect/BD meal)
  are common. Can't tell from the merchant? Default `Groceries & Meals`, low conf.
- **Amazon / general retail**: often `Marketing - Book` (book promo materials,
  giveaways) or `Office Supplies (NY & Home)`. If unclear, `Office Supplies`.
- **AI / engineering tools** (Anthropic, OpenAI, Granola, Netlify, Supabase,
  Cursor, Vercel): `R&D AI Everyday (Internal Tools & Training)` — NOT generic
  Web Services. (These usually hit a rule before reaching you.)
- **Book print & fulfillment** (IngramSpark, Box Genie, Noissue, StickerGiant,
  Porchlight, shipping for book materials): `Book Development and Publishing`.
- **News / industry subscriptions & courses** (coaching, academies, Chief):
  `Professional Development`.
- **General SaaS / software**: `Web Services & Subscriptions`.

## Rules of thumb
- Prefer the more specific category over a generic one when the merchant
  clearly signals it (a known AI tool is R&D AI, not Web Services).
- Never invent a category — only use names from the provided list verbatim.
- `Billable Expense` means it gets re-billed to a client. Don't guess billable
  without a clear client/engagement signal; when unsure, pick the non-billable
  category and let the reviewer flip it.
