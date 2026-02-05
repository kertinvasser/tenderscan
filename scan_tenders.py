name: Scan EU Funding & Tenders

on:
  schedule:
    # Runs daily at 06:15 UTC (adjust as you want)
    - cron: "15 6 * * *"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run scan
        env:
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          EMAIL_TO:   ${{ secrets.EMAIL_TO }}
          EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
        run: |
          python scan_tenders.py

      - name: Commit updated sent_ids.json
        run: |
          if git diff --quiet; then
            echo "No state changes."
            exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add sent_ids.json
          git commit -m "Update sent tenders state"
          git push
