# Re-record from the repo root with  vhs docs/demo.tape  after ./run.sh has created .venv
Output "docs/demo.mp4"

Set Shell "bash"
Set FontSize 17
Set Width 1500
Set Height 1400
Set Padding 24
Set Theme "Dracula"
Set Framerate 30
Set TypingSpeed 35ms

Hide
Type "source .venv/bin/activate && export PS1='$ ' && D=$(mktemp -d /tmp/demo-run.XXXXXX) && export DB_PATH=$D/d.db LEDGER_PATH=$D/l.jsonl LOG_FILE=$D/app.log LLM_MODE=offline PORT=8123 API_URL=http://127.0.0.1:8123"
Enter
Type "(python -m uvicorn app.main:app --port 8123 --log-level warning >$D/srv.out 2>&1 &) ; sleep 3 ; python -m app.cli health ; clear"
Enter
Sleep 5s
Show

Type "# Customer success manager. Load the feedback, then just ask."
Enter
Sleep 1s
Type "python -m app.cli ingest data/sample_feedback.json"
Enter
Sleep 2s
Type "python -m app.cli analyze"
Enter
Sleep 4s
Type "clear"
Enter
Type "python -m app.cli chat"
Enter
Sleep 2s
Type "Which accounts need attention today?"
Enter
Sleep 9s
Type "yes, it caught the Acme Friday renewal ultimatum and the Umbrella go-live"
Enter
Sleep 3s
