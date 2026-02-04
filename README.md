# The Drop

Automated triweekly newsletter generator. Fetches newsletters from Gmail, generates curated content using Claude, and sends a polished HTML email.

**Schedule:** Monday, Wednesday, Friday at 7:00 AM ET

## Setup

### 1. Gmail API Setup

#### Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project named "the-drop-automation"
3. Enable the Gmail API:
   - Navigate to "APIs & Services" > "Library"
   - Search for "Gmail API"
   - Click "Enable"

#### Create OAuth Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. If prompted, configure the OAuth consent screen:
   - User Type: External
   - App name: "The Drop"
   - Add your email as a test user
4. For OAuth client ID:
   - Application type: "Desktop app"
   - Name: "The Drop"
5. Download the JSON file and save it as `credentials.json` in this directory

### 2. Gmail Labels

Create these labels in Gmail to organize your newsletter subscriptions:

```
Newsletters/
├── Events
├── Finance-Wealth
├── GenTech
├── HealthTech
└── Personal
```

Set up filters to automatically label incoming newsletters.

### 3. Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template and fill in your values
cp .env.example .env
```

Edit `.env` with your values:
- `ANTHROPIC_API_KEY`: Your Anthropic API key
- `SEND_TO`: Email address to receive the newsletter

### 4. First-Time Authentication

Run the script once locally to complete OAuth:

```bash
python the_drop.py
```

This will open a browser window for Google OAuth. After authenticating, a `token.json` file will be created.

### 5. GitHub Actions Setup (for automated runs)

1. Create a GitHub repository and push this code
2. Add these secrets in repository Settings > Secrets and variables > Actions:
   - `ANTHROPIC_API_KEY`: Your Anthropic API key
   - `SEND_TO`: Recipient email address
   - `GMAIL_TOKEN`: Contents of `token.json` (copy the entire file)
   - `GMAIL_CREDENTIALS`: Contents of `credentials.json` (copy the entire file)

The workflow will run automatically Mon/Wed/Fri at 7am ET.

## Manual Trigger

### Local
```bash
source venv/bin/activate
python the_drop.py
```

### GitHub Actions
Go to Actions tab > "Generate The Drop" > "Run workflow"

## Newsletter Sources

### Tier 1 (Primary)
- Exec Sum
- Axios Markets / Axios Pro Rata
- Endpoints News
- STAT News
- The Information
- Term Sheet (Fortune)
- StrictlyVC

### Tier 2 (Supplementary)
- Money Stuff (Matt Levine)
- Stratechery
- BioCentury
- Fierce Pharma
- Import AI
- Ben's Bites
- PE Hub
- Pitchbook
- Lenny's Newsletter

### Tier 3 (Lifestyle/Culture)
- Eater NY
- Infatuation
- Time Out
- Garbage Day
- After School
- The Athletic

## Cost Estimates

- **Anthropic API**: ~$0.05-0.10 per issue ($2-4/month)
- **Gmail API**: Free tier
- **GitHub Actions**: Free tier (~30-40 min/month)

## Files

| File | Description |
|------|-------------|
| `the_drop.py` | Main automation script |
| `the-drop-claude-prompt.md` | Claude system prompt |
| `the-drop-template-v2.html` | HTML email template |
| `requirements.txt` | Python dependencies |
| `.env` | Environment variables (not committed) |
| `.env.example` | Environment template |
| `credentials.json` | Gmail OAuth credentials (not committed) |
| `token.json` | Gmail auth token (not committed) |

## Troubleshooting

### "Newsletters label not found"
Create the "Newsletters" label in Gmail, or the script will search all unread emails.

### OAuth errors
Delete `token.json` and run the script again to re-authenticate.

### No emails fetched
Check that newsletters are arriving in Gmail and being labeled correctly.
