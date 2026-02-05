#!/usr/bin/env python3
"""
The Drop - Automated Newsletter Generator

Fetches newsletters from Gmail, generates curated content using Claude,
assembles HTML email, and sends it.
"""

import os
import base64
import re
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

# Configuration
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-20250514')
SEND_TO = os.getenv('SEND_TO', '')
SCRIPT_DIR = Path(__file__).parent

# Dual OAuth file paths
# Source account (rojish99@gmail.com) - for reading newsletters
SOURCE_TOKEN = SCRIPT_DIR / 'token-source.json'
SOURCE_CREDENTIALS = SCRIPT_DIR / 'credentials-source.json'
# Sender account (rohanshah59@gmail.com) - for sending The Drop
SENDER_TOKEN = SCRIPT_DIR / 'token-sender.json'
SENDER_CREDENTIALS = SCRIPT_DIR / 'credentials-sender.json'

# Fixed header/hero image
HEADER_BG_IMAGE = os.getenv('HEADER_BG_IMAGE', 'https://raw.githubusercontent.com/r8slab/the-drop/main/assets/hero-background-wide.jpg')

# NYC Callout template (used when there's a new opening)
NYC_CALLOUT_TEMPLATE = '''<tr>
    <td style="padding: 0 0 20px 0;">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" class="section-card" style="background: linear-gradient(135deg, #134E4A 0%, #0F766E 100%); border-radius: 12px; border: 1px solid #14B8A6;">
        <tr>
          <td class="content-padding" style="padding: 20px 28px;">
            <p style="margin: 0 0 6px 0; font-size: 11px; font-weight: 600; color: #5EEAD4; text-transform: uppercase; letter-spacing: 0.12em;">New Opening</p>
            <p style="margin: 0; font-size: 15px; color: #F0FDFA; line-height: 1.6;">
              {content}
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>'''

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class TheDropGenerator:
    """Main class for generating The Drop newsletter."""

    def __init__(self):
        # Authenticate with both Gmail accounts
        logger.info("Authenticating with source account (rojish99@gmail.com)...")
        self.gmail_source = self._authenticate_gmail(SOURCE_TOKEN, SOURCE_CREDENTIALS, "source")
        logger.info("Authenticating with sender account (rohanshah59@gmail.com)...")
        self.gmail_sender = self._authenticate_gmail(SENDER_TOKEN, SENDER_CREDENTIALS, "sender")
        self.claude = anthropic.Anthropic()
        self.last_run_time = self._get_last_run_time()

    def _authenticate_gmail(self, token_path, credentials_path, account_type):
        """Authenticate with Gmail API using OAuth."""
        creds = None

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info(f"Refreshing expired credentials for {account_type} account...")
                creds.refresh(Request())
            else:
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"{credentials_path.name} not found at {credentials_path}. "
                        f"Please download OAuth credentials for the {account_type} account from Google Cloud Console."
                    )
                logger.info(f"Running OAuth flow for {account_type} account...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(token_path, 'w') as token:
                token.write(creds.to_json())
                logger.info(f"Token saved to {token_path}")

        return build('gmail', 'v1', credentials=creds)

    def _get_last_run_time(self):
        """Get timestamp of last successful run."""
        last_run_path = SCRIPT_DIR / '.last_run'
        try:
            with open(last_run_path, 'r') as f:
                return datetime.fromisoformat(f.read().strip())
        except FileNotFoundError:
            # First run: get last 3 days for initial content
            return datetime.now() - timedelta(days=3)

    def _save_last_run_time(self):
        """Save current timestamp for next run."""
        last_run_path = SCRIPT_DIR / '.last_run'
        with open(last_run_path, 'w') as f:
            f.write(datetime.now().isoformat())

    def fetch_newsletters(self, days_back=None, include_read=False):
        """Fetch emails from Newsletters label and all sublabels (from source account).

        Args:
            days_back: If set, fetch emails from the last N days instead of since last run
            include_read: If True, include both read and unread emails
        """
        # Get all labels that start with "Newsletters"
        labels = self.gmail_source.users().labels().list(userId='me').execute()
        newsletter_labels = [
            l['name'] for l in labels['labels']
            if l['name'] == 'Newsletters' or l['name'].startswith('Newsletters/')
        ]

        if not newsletter_labels:
            logger.warning("No Newsletter labels found in source account. Searching all emails.")
            query = ""
        else:
            # Build OR query with curly braces for all newsletter labels
            # Gmail syntax: {label:A label:B} means A OR B
            label_parts = [f'label:"{l}"' for l in newsletter_labels]
            or_query = '{' + ' '.join(label_parts) + '}'

            # Determine time window
            if days_back:
                after_time = datetime.now() - timedelta(days=days_back)
                after_timestamp = int(after_time.timestamp())
                logger.info(f"Fetching emails from last {days_back} days")
            else:
                after_timestamp = int(self.last_run_time.timestamp())
                logger.info(f"Fetching emails since last run")

            # Build query - optionally include read emails
            if include_read:
                query = f"{or_query} after:{after_timestamp}"
            else:
                query = f"{or_query} is:unread after:{after_timestamp}"

            logger.info(f"Found {len(newsletter_labels)} newsletter labels")

        logger.info(f"Fetching emails from source account with query: {query}")

        # Fetch messages (limit to 30 to stay within token limits)
        results = self.gmail_source.users().messages().list(
            userId='me',
            q=query,
            maxResults=35
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg in messages:
            try:
                full_msg = self.gmail_source.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='full'
                ).execute()
                parsed = self._parse_email(full_msg)
                if parsed:
                    emails.append(parsed)
            except Exception as e:
                logger.error(f"Error parsing email {msg['id']}: {e}")
                continue

        return emails

    def _parse_email(self, msg):
        """Parse email into structured format."""
        headers = {h['name']: h['value'] for h in msg['payload']['headers']}

        # Get body
        body = self._get_email_body(msg['payload'])
        if not body:
            return None

        # Extract images
        images = self._extract_images(body)

        # Parse HTML content
        soup = BeautifulSoup(body, 'lxml')
        text = soup.get_text(separator='\n', strip=True)
        links = [(a.get_text(strip=True), a.get('href'))
                 for a in soup.find_all('a', href=True)
                 if a.get('href') and not a.get('href').startswith('mailto:')]

        return {
            'id': msg['id'],
            'from': headers.get('From', ''),
            'subject': headers.get('Subject', ''),
            'date': headers.get('Date', ''),
            'text': text,
            'html': body,
            'links': links[:30],  # Limit links to avoid token explosion
            'images': images
        }

    def _get_email_body(self, payload):
        """Extract email body from payload."""
        if 'body' in payload and payload['body'].get('data'):
            return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')

        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/html':
                    if part['body'].get('data'):
                        return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                elif part['mimeType'].startswith('multipart/'):
                    result = self._get_email_body(part)
                    if result:
                        return result

        return ""

    def _extract_images(self, html):
        """Extract image URLs from email HTML."""
        images = []
        soup = BeautifulSoup(html, 'lxml')
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if src and not src.startswith('data:'):
                images.append({
                    'src': src,
                    'alt': img.get('alt', '')
                })
        return images[:10]  # Limit to avoid noise

    def _extract_exec_sum_market_image(self, emails):
        """Extract market snapshot table image from Exec Sum email.

        The market table image appears under "Before the Bell" section in Exec Sum.
        """
        for email in emails:
            from_addr = email['from'].lower()
            subject = email['subject'].lower()

            # Identify Exec Sum email
            is_exec_sum = ('exec' in from_addr or 'execsum' in from_addr or
                           'executive summary' in subject or 'exec sum' in subject)

            if not is_exec_sum:
                continue

            logger.info(f"Found Exec Sum email: {email['subject']}")

            # Parse HTML to find images near "Before the Bell" section
            soup = BeautifulSoup(email['html'], 'lxml')

            # Strategy 1: Find image after "Before the Bell" text
            before_bell_patterns = [
                re.compile(r'before\s*the\s*bell', re.I),
                re.compile(r'market\s*snapshot', re.I),
                re.compile(r'markets\s*at\s*a\s*glance', re.I),
            ]

            for pattern in before_bell_patterns:
                headings = soup.find_all(string=pattern)
                for heading in headings:
                    # Find the parent container
                    parent = heading.find_parent(['td', 'tr', 'table', 'div'])
                    if parent:
                        # Look for the next image in the document flow
                        img = parent.find_next('img')
                        if img and img.get('src'):
                            src = img.get('src')
                            # Skip tiny images (logos, icons, spacers)
                            if 'logo' not in src.lower() and 'icon' not in src.lower():
                                logger.info(f"Found market image near '{pattern.pattern}': {src[:80]}...")
                                return src

            # Strategy 2: Look for images with market-related alt text
            for img in soup.find_all('img'):
                alt = (img.get('alt') or '').lower()
                if any(kw in alt for kw in ['market', 'futures', 'indices', 'stocks', 'bell']):
                    src = img.get('src')
                    if src:
                        logger.info(f"Found market image by alt text '{alt}': {src[:80]}...")
                        return src

            # Strategy 3: Look for large images (likely the market table)
            # Skip images that are clearly logos/icons based on URL patterns
            for img in email['images']:
                src = img.get('src', '')
                alt = img.get('alt', '').lower()
                # Skip logos, icons, social media buttons
                skip_patterns = ['logo', 'icon', 'button', 'social', 'twitter', 'facebook', 'linkedin', 'spacer', '1x1']
                if any(pattern in src.lower() or pattern in alt for pattern in skip_patterns):
                    continue
                # This might be the market table
                logger.info(f"Fallback: Using image {src[:80]}...")
                return src

        logger.warning("No Exec Sum market image found")
        return None

    def generate_newsletter(self, emails):
        """Send emails to Claude for newsletter generation."""
        # Load the prompt
        prompt_path = SCRIPT_DIR / 'the-drop-claude-prompt.md'
        with open(prompt_path, 'r') as f:
            system_prompt = f.read()

        # Prepare email content for Claude
        email_summaries = []
        for email in emails:
            # Truncate long emails to manage tokens (more aggressive)
            text = email['text'][:2000] if len(email['text']) > 2000 else email['text']
            links_text = '\n'.join([f"- {t}: {url}" for t, url in email['links'][:10]])

            email_summaries.append(f"""
---
FROM: {email['from']}
SUBJECT: {email['subject']}
DATE: {email['date']}

CONTENT:
{text}

LINKS:
{links_text}
---
""")

        today = datetime.now()
        day_name = today.strftime('%A')

        user_message = f"""
Today is {today.strftime('%A, %B %d, %Y')}.

This is a {'Monday' if day_name == 'Monday' else 'mid-week'} issue, so it should be {'denser (8-10 min read, 55-65 items)' if day_name == 'Monday' else 'lighter (5-7 min read, 40-50 items)'}.

Here are the newsletter emails received since the last issue:

{''.join(email_summaries)}

Please generate today's issue of The Drop based on these sources. Output the content for each section in a structured format that I can inject into the HTML template.

Format your response as:

## EMAIL_SUBJECT
[punchy subject line, max 60 chars, format: "Today's Drop: [headline]"]

## GOOD_MORNING
[content]

## BEFORE_THE_BELL_MARKETS
[bullets]

## BEFORE_THE_BELL_EARNINGS_LAST
[bullets]

## BEFORE_THE_BELL_EARNINGS_UPCOMING
[content]

## HEADLINE_ROUNDUP
[bullets]

## PHARMA_HEALTH_INTEL
[bullets]

## TECH_AI
[bullets]

## DEAL_FLOW_MA
[bullets]

## DEAL_FLOW_VENTURE
[bullets]

## DEAL_FLOW_IPO
[bullets]

## DEAL_FLOW_SCOUTING
[bullets with "Why it matters"]

## NYC_EVENTS
[bullets]

## NYC_RESTAURANT
[recommendation]

## NYC_CALLOUT
[optional: new opening or special callout, or "NONE"]

## CULTURE_SPORTS
[bullets]

## CULTURE_MEME
[description and context for meme of the week]

## CULTURE_INTERNET
[bullets]

## READS_OF_THE_WEEK
[bullets with source and one-line description]
"""

        logger.info("Calling Claude API for newsletter generation...")
        response = self.claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )

        return response.content[0].text

    def parse_claude_response(self, response):
        """Parse Claude's structured response into sections."""
        sections = {}
        current_section = None
        current_content = []

        for line in response.split('\n'):
            if line.startswith('## '):
                if current_section:
                    sections[current_section] = '\n'.join(current_content).strip()
                current_section = line[3:].strip()
                current_content = []
            else:
                current_content.append(line)

        if current_section:
            sections[current_section] = '\n'.join(current_content).strip()

        logger.info(f"Parsed sections: {list(sections.keys())}")
        return sections

    def _bullets_to_html(self, content, accent_color='#818CF8'):
        """Convert markdown bullets to HTML list items."""
        lines = content.strip().split('\n')
        html_bullets = []

        for line in lines:
            line = line.strip()
            if line.startswith('- ') or line.startswith('* '):
                line = line[2:]
            elif line.startswith('• '):
                line = line[2:]
            if not line:
                continue

            # Convert **text** to <strong>
            line = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color: #FFFFFF;">\1</strong>', line)
            # Convert [text](url) to links
            line = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2" style="color: #818CF8;">\1</a>', line)

            html_bullets.append(f'''<li style="margin-bottom: 12px; padding-left: 16px; position: relative; color: #E4E4E7; font-size: 15px; line-height: 1.6;">
                <span style="position: absolute; left: 0; color: {accent_color};">›</span>
                {line}
              </li>''')

        return '\n'.join(html_bullets)

    def _format_paragraph(self, content):
        """Format plain text content, converting markdown links."""
        content = content.strip()
        # Convert **text** to <strong>
        content = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color: #FFFFFF;">\1</strong>', content)
        # Convert [text](url) to links
        content = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2" style="color: #818CF8;">\1</a>', content)
        return content

    def _format_scouting_picks(self, content):
        """Format scouting picks with special styling."""
        content = content.strip()
        # Convert **text** to <strong>
        content = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color: #FFFFFF;">\1</strong>', content)
        # Convert [text](url) to links
        content = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2" style="color: #A5B4FC;">\1</a>', content)

        # Look for "Why it matters:" and style it
        if 'Why it matters:' in content:
            parts = content.split('Why it matters:', 1)
            return f'''<p style="margin: 0 0 8px 0; font-size: 15px; color: #E4E4E7; line-height: 1.6;">
                {parts[0].strip()}
              </p>
              <p style="margin: 0; font-size: 14px; color: #A5B4FC; line-height: 1.5;">
                Why it matters: {parts[1].strip()}
              </p>'''
        return f'<p style="margin: 0; font-size: 15px; color: #E4E4E7; line-height: 1.6;">{content}</p>'

    def _format_reads(self, content, accent_color='#60A5FA'):
        """Format reads of the week with special styling for titles and descriptions.

        Expected format from Claude:
        - **[Article Title](URL)** · Source Name · Description text
        """
        lines = content.strip().split('\n')
        html_items = []

        for line in lines:
            line = line.strip()
            if line.startswith('- ') or line.startswith('* '):
                line = line[2:]
            elif line.startswith('• '):
                line = line[2:]
            if not line:
                continue

            # Check for paywall marker
            paywall_tag = ''
            if '[Paywall]' in line or '[paywall]' in line:
                paywall_tag = '<span style="display: inline-block; background-color: #3F3F46; color: #A1A1AA; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">Paywall</span>'
                line = line.replace('[Paywall]', '').replace('[paywall]', '').strip()

            # Try to parse format: **[Title](URL)** · Source · Description
            # Pattern 1: **[Title](URL)** · Source · Description
            pattern1 = re.match(r'\*\*\[(.+?)\]\((.+?)\)\*\*\s*·\s*(.+?)\s*·\s*(.+)', line)
            # Pattern 2: **[Title](URL)** · Source (no description)
            pattern2 = re.match(r'\*\*\[(.+?)\]\((.+?)\)\*\*\s*·\s*(.+)', line)
            # Pattern 3: **Title** · Source · Description (no link in title)
            pattern3 = re.match(r'\*\*(.+?)\*\*\s*·\s*(.+?)\s*·\s*(.+)', line)
            # Pattern 4: [Title](URL) · Source · Description (no bold)
            pattern4 = re.match(r'\[(.+?)\]\((.+?)\)\s*·\s*(.+?)\s*·\s*(.+)', line)

            title = ''
            link_url = '#'
            source = ''
            description = ''

            if pattern1:
                title = pattern1.group(1)
                link_url = pattern1.group(2)
                source = pattern1.group(3).strip()
                description = pattern1.group(4).strip()
            elif pattern2:
                title = pattern2.group(1)
                link_url = pattern2.group(2)
                rest = pattern2.group(3).strip()
                # Check if rest contains another · for description
                if '·' in rest:
                    parts = rest.split('·', 1)
                    source = parts[0].strip()
                    description = parts[1].strip() if len(parts) > 1 else ''
                else:
                    source = rest
            elif pattern3:
                title = pattern3.group(1)
                source = pattern3.group(2).strip()
                description = pattern3.group(3).strip()
                # Check for link in description
                link_match = re.search(r'\[.+?\]\((.+?)\)', line)
                if link_match:
                    link_url = link_match.group(1)
            elif pattern4:
                title = pattern4.group(1)
                link_url = pattern4.group(2)
                source = pattern4.group(3).strip()
                description = pattern4.group(4).strip()
            else:
                # Fallback: just use the whole line
                title = line
                # Try to extract any link
                link_match = re.search(r'\[.+?\]\((.+?)\)', line)
                if link_match:
                    link_url = link_match.group(1)

            # Clean up any remaining markdown from description
            description = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1', description)

            html_items.append(f'''<li style="margin-bottom: 16px; padding-left: 16px; position: relative;">
                <span style="position: absolute; left: 0; color: {accent_color};">›</span>
                <a href="{link_url}" style="color: #FFFFFF; font-size: 15px; font-weight: 600; text-decoration: none;">{title}</a>
                <span style="color: #71717A; font-size: 14px;"> · {source}</span>
                {paywall_tag}
                <p style="margin: 6px 0 0 0; font-size: 14px; color: #A1A1AA; line-height: 1.5;">{description}</p>
              </li>''')

        return '\n'.join(html_items)

    def assemble_html(self, sections, market_image_url):
        """Inject sections into HTML template."""
        template_path = SCRIPT_DIR / 'the-drop-template-v2.html'
        with open(template_path, 'r') as f:
            template = f.read()

        # Replace date
        today = datetime.now()
        template = template.replace('{{DATE}}', today.strftime('%A, %B %d, %Y'))

        # Replace header image
        template = template.replace('{{HEADER_BG_IMAGE}}', HEADER_BG_IMAGE)

        # Replace market image
        if market_image_url:
            template = template.replace('{{EXEC_SUM_MARKET_IMAGE_URL}}', market_image_url)
        else:
            template = template.replace('{{EXEC_SUM_MARKET_IMAGE_URL}}', '')

        # Section mappings: (placeholder, accent_color, format_type)
        # format_type: 'bullets', 'paragraph', 'scouting', 'reads'
        section_mappings = {
            'GOOD_MORNING': ('{{GOOD_MORNING_CONTENT}}', None, 'paragraph'),
            'BEFORE_THE_BELL_MARKETS': ('{{BEFORE_THE_BELL_MARKETS}}', '#34D399', 'bullets'),
            'BEFORE_THE_BELL_EARNINGS_LAST': ('{{BEFORE_THE_BELL_EARNINGS_LAST}}', '#34D399', 'bullets'),
            'BEFORE_THE_BELL_EARNINGS_UPCOMING': ('{{BEFORE_THE_BELL_EARNINGS_UPCOMING}}', None, 'paragraph'),
            'HEADLINE_ROUNDUP': ('{{HEADLINE_ROUNDUP}}', '#F472B6', 'bullets'),
            'PHARMA_HEALTH_INTEL': ('{{PHARMA_HEALTH_INTEL}}', '#22D3EE', 'bullets'),
            'TECH_AI': ('{{TECH_AI}}', '#FBBF24', 'bullets'),
            'DEAL_FLOW_MA': ('{{DEAL_FLOW_MA}}', '#FB923C', 'bullets'),
            'DEAL_FLOW_VENTURE': ('{{DEAL_FLOW_VENTURE}}', '#FB923C', 'bullets'),
            'DEAL_FLOW_SCOUTING': ('{{DEAL_FLOW_SCOUTING}}', None, 'scouting'),
            'NYC_EVENTS': ('{{NYC_EVENTS}}', '#4ADE80', 'bullets'),
            'NYC_RESTAURANT': ('{{NYC_RESTAURANT}}', None, 'paragraph'),
            'CULTURE_SPORTS': ('{{CULTURE_SPORTS}}', '#E879F9', 'bullets'),
            'CULTURE_MEME': ('{{CULTURE_MEME}}', None, 'paragraph'),
            'CULTURE_INTERNET': ('{{CULTURE_INTERNET}}', '#E879F9', 'bullets'),
            'READS_OF_THE_WEEK': ('{{READS_OF_THE_WEEK}}', '#60A5FA', 'reads'),
        }

        for section_name, (placeholder, accent_color, format_type) in section_mappings.items():
            content = sections.get(section_name, '')
            if not content:
                html_content = ''
            elif format_type == 'bullets':
                html_content = self._bullets_to_html(content, accent_color)
            elif format_type == 'paragraph':
                html_content = self._format_paragraph(content)
            elif format_type == 'scouting':
                html_content = self._format_scouting_picks(content)
            elif format_type == 'reads':
                html_content = self._format_reads(content, accent_color)
            else:
                html_content = content

            template = template.replace(placeholder, html_content)

        # Handle NYC Callout (optional section)
        nyc_callout = sections.get('NYC_CALLOUT', '')
        if nyc_callout and nyc_callout.strip().upper() != 'NONE':
            callout_html = NYC_CALLOUT_TEMPLATE.format(content=self._format_paragraph(nyc_callout))
            template = template.replace('{{NYC_CALLOUT_SECTION}}', callout_html)
        else:
            template = template.replace('{{NYC_CALLOUT_SECTION}}', '')

        logger.info("HTML template assembled with dynamic content")
        return template

    def send_email(self, html_content, subject=None):
        """Send the newsletter via Gmail (using sender account)."""
        if not SEND_TO:
            raise ValueError("SEND_TO environment variable not set")

        message = MIMEMultipart('alternative')
        message['to'] = SEND_TO
        # Use dynamic subject from Claude if provided, otherwise fallback
        if subject:
            message['subject'] = subject
        else:
            message['subject'] = f"Today's Drop: {datetime.now().strftime('%B %d')}"

        # Attach HTML
        html_part = MIMEText(html_content, 'html')
        message.attach(html_part)

        # Encode and send using sender account
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        self.gmail_sender.users().messages().send(
            userId='me',
            body={'raw': raw}
        ).execute()

        logger.info(f"Email sent to {SEND_TO} from sender account")

    def mark_as_read(self, emails):
        """Mark processed emails as read (in source account)."""
        for email in emails:
            try:
                self.gmail_source.users().messages().modify(
                    userId='me',
                    id=email['id'],
                    body={'removeLabelIds': ['UNREAD']}
                ).execute()
            except Exception as e:
                logger.error(f"Error marking email {email['id']} as read: {e}")

        logger.info(f"Marked {len(emails)} emails as read in source account")

    def _send_failure_notification(self, error_msg):
        """Send notification if generation fails (using sender account)."""
        if not SEND_TO:
            logger.error("Cannot send failure notification: SEND_TO not set")
            return

        message = MIMEText(f"The Drop failed to generate.\n\nError: {error_msg}")
        message['to'] = SEND_TO
        message['subject'] = "The Drop: Generation Failed"

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        try:
            self.gmail_sender.users().messages().send(
                userId='me',
                body={'raw': raw}
            ).execute()
            logger.info("Failure notification sent")
        except Exception as e:
            logger.error(f"Failed to send failure notification: {e}")

    def run(self, days_back=None, include_read=False):
        """Main execution flow.

        Args:
            days_back: If set, fetch emails from the last N days instead of since last run
            include_read: If True, include both read and unread emails
        """
        try:
            logger.info("Starting The Drop generation...")

            # 1. Fetch emails
            logger.info("Fetching newsletters...")
            emails = self.fetch_newsletters(days_back=days_back, include_read=include_read)
            logger.info(f"Found {len(emails)} newsletters")

            if not emails:
                logger.info("No new emails to process. Exiting.")
                return

            # 2. Extract market image
            market_image = self._extract_exec_sum_market_image(emails)
            if market_image:
                logger.info(f"Found market image: {market_image[:50]}...")

            # 3. Generate with Claude
            logger.info("Generating newsletter with Claude...")
            claude_response = self.generate_newsletter(emails)

            # 4. Parse response
            sections = self.parse_claude_response(claude_response)
            logger.info(f"Parsed {len(sections)} sections from Claude response")

            # 5. Assemble HTML
            logger.info("Assembling HTML...")
            html = self.assemble_html(sections, market_image)

            # 6. Send email
            email_subject = sections.get('EMAIL_SUBJECT', '').strip()
            logger.info(f"Sending email with subject: {email_subject or 'default'}")
            self.send_email(html, subject=email_subject if email_subject else None)

            # 7. Mark as read
            logger.info("Marking emails as read...")
            self.mark_as_read(emails)

            # 8. Save timestamp
            self._save_last_run_time()

            logger.info("The Drop sent successfully!")

        except Exception as e:
            logger.error(f"ERROR: {e}")
            self._send_failure_notification(str(e))
            raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Generate The Drop newsletter')
    parser.add_argument('--preview', action='store_true',
                        help='Generate HTML preview without sending email')
    parser.add_argument('--preview-file', type=str, default='preview.html',
                        help='Output file for preview (default: preview.html)')
    parser.add_argument('--days', type=int, default=None,
                        help='Fetch emails from the last N days (default: since last run)')
    parser.add_argument('--include-read', action='store_true',
                        help='Include read emails (default: unread only)')
    args = parser.parse_args()

    generator = TheDropGenerator()

    if args.preview:
        # Preview mode: generate HTML and save to file, don't send
        logger.info("Running in PREVIEW mode - no email will be sent")
        logger.info("Fetching newsletters...")
        emails = generator.fetch_newsletters(days_back=args.days, include_read=args.include_read)
        logger.info(f"Found {len(emails)} newsletters")

        if emails:
            market_image = generator._extract_exec_sum_market_image(emails)
            logger.info("Generating newsletter with Claude...")
            claude_response = generator.generate_newsletter(emails)
            sections = generator.parse_claude_response(claude_response)
            logger.info(f"Parsed {len(sections)} sections")

            # Show the dynamic subject line
            email_subject = sections.get('EMAIL_SUBJECT', '').strip()
            if email_subject:
                logger.info(f"EMAIL SUBJECT: {email_subject}")
            else:
                logger.warning("No EMAIL_SUBJECT generated, will use default")

            html = generator.assemble_html(sections, market_image)

            # Save to file
            preview_path = SCRIPT_DIR / args.preview_file
            with open(preview_path, 'w') as f:
                f.write(html)
            logger.info(f"Preview saved to: {preview_path}")
            logger.info(f"Open in browser: file://{preview_path}")
        else:
            logger.info("No emails to process")
    else:
        generator.run(days_back=args.days, include_read=args.include_read)
