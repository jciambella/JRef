# Ask AI: connecting a language model

**Study mode** (select a reference ▸ **Study and ask AI** at the bottom of the reference sheet, or `Ctrl+J`;
`Esc` returns) shows the paper's notes on the left and **Ask AI** on the right. JRef sends the paper's text (with
page markers) or, if you allow it, the PDF file itself, together with the text of its supplementary PDFs, to the
provider you choose; the answer streams into the panel, you can ask follow-up questions and **Save to Notes**
appends the exchange to the paper's notes.

## Before you start: API use is billed separately

Ask AI calls the provider's **API** with **your own API key**; the provider bills you per use. A ChatGPT,
Claude or Gemini *subscription* does not include API use (see OpenAI's help article *"Is API usage included in
ChatGPT Plus?"*). If you only have a subscription, use **Ask in browser** in the panel: JRef copies the prompt,
opens the provider's chat site and shows you the PDF to drag into it. A local model through Ollama needs no key
and nothing leaves your computer.

## OpenAI, step by step

1. **Sign in to the API platform** at <https://platform.openai.com> (the same account as ChatGPT can be used,
   but billing is separate).
2. **Add billing.** In the platform's organisation settings open *Billing* and add a payment method or credit.
   Requests fail until the organisation has credit.
3. **Optional, recommended: a project with a budget.** Create a project called e.g. `JRef` and set its *monthly
   budget*. The budget is a hard cap: once the project exceeds it in a calendar month (UTC), further requests
   are blocked. Only organisation owners can create projects.
4. **Create the key** at <https://platform.openai.com/api-keys>, in the project of step 3. The default
   permission (*All*) works with JRef, which only lists models and sends chat requests. Copy the key and keep
   it private: anyone holding it can spend your credit.
5. **Enter it in JRef.** Select a paper ▸ **Study and ask AI** (`Ctrl+J`) ▸ gear button next to the model box
   (*API keys and options*) ▸ *Provider*: **ChatGPT models (OpenAI)** ▸ paste the key into *API key* ▸ **Save**.
6. **Choose a model.** Press the refresh button next to the model box: JRef fetches the list of models your key
   can use. Pick one, or type a model name.
7. **Ask.** Before the first question to a cloud provider JRef shows, once, what will be sent and where. Answer
   *Yes* to continue.

**Alternative to step 5:** if the environment variable `OPENAI_API_KEY` is set (OpenAI's quickstart uses
`setx OPENAI_API_KEY "your_api_key_here"` in PowerShell), JRef uses it when no key is stored in the dialog.

### Where the key is kept

Never in `config.json` and never in the (possibly synchronised) library folder:

- **Windows**: encrypted with DPAPI, i.e. with your Windows login, in `%APPDATA%\JRef\secrets.json`. Another
  Windows account, or a copy of the file on another computer, cannot decrypt it.
- **macOS / Linux**: the system keyring if the `keyring` package is installed and working, otherwise
  `secrets.json` readable by your user only.

Each computer therefore needs the key entered once. The settings dialog says which storage is in use.

## Options in the settings dialog

| Option | Effect |
|---|---|
| *Send the PDF file itself* | The model sees figures, tables and equations as printed. More tokens per question. |
| *Include the text of supplementary PDFs* | Adds the text of the paper's supplementary PDFs. |
| *Paper text sent at most* | Upper bound in thousands of characters (about 4 characters per token). |
| *Answer length at most* | Upper bound on the answer, in tokens. |

The line under the provider box tells you how much paper text goes out with each question. Each question
sends it again, so long conversations about long papers cost more; the project budget of step 3 keeps that in
check.

## Other providers

| Provider in JRef | Key | Notes |
|---|---|---|
| Claude (Anthropic) | <https://platform.claude.com/settings/keys> | A *Claude workspace ID* is needed only for keys not scoped to one workspace. |
| Gemini (Google) | <https://aistudio.google.com/apikey> | On the unpaid tier Google may use what you send to improve its products and human reviewers may read it. |
| Local model (Ollama) | none | Install Ollama (<https://ollama.com/download>) and pull a model; nothing leaves the computer. |
| Other (OpenAI-compatible server) | optional | Any server with an OpenAI-compatible `/chat/completions` endpoint; enter its *Server address*. |

## Privacy

The paper's text (or the PDF) and your questions are sent to the provider under its terms. **Do not send
confidential documents**, such as manuscripts you are reviewing: journals and funders generally forbid uploading
them to AI services. Use a local model for those.
