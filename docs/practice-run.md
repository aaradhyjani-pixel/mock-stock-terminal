# Running a practice session on your Wi-Fi

Everyone must be on the same Wi-Fi as the host laptop. Nothing here needs the
internet, an account, or a card.

## Handing out the address

```bash
cd "/Users/aaradhymanojkumarjani/MOCK STOCK TERMINAL" && ./link
```

That prints the current addresses. Give players the `.local` one:

    http://Leverage.local:8000

It keeps working even when the laptop's IP changes, which it does. The numeric
address is printed as a fallback for any phone whose browser will not resolve
`.local` (some older Androids).

## It stays up on its own

A launchd service runs the server. It restarts on failure and starts at login,
so it survives closing the terminal, logging out and rebooting.

    launchctl list | grep mockstock          # check
    launchctl unload ~/Library/LaunchAgents/com.mockstock.terminal.plist   # stop
    launchctl load   ~/Library/LaunchAgents/com.mockstock.terminal.plist   # start

The laptop must stay awake and on the Wi-Fi. Nothing else.

## Running the session

1. Open the console at `/console/login` as **director / admin123**.
2. Check the market says OPEN, day 1.
3. Hand out `practice-logins.html`, one row per person. All passwords are
   `trade123`.
4. Put `/projector` on a screen if you have one.

Then try this, in order, to see the whole thing work:

- Everyone buys something.
- As **market**, move a stock 6% over 60 seconds. Watch it move on their phones.
- As **news**, publish a headline. A toast appears on every screen.
- Have someone short 5,000 shares of a cheap stock, then push that price up 10%.
  They get a margin warning, then the exchange buys it back automatically.
- As **director**, press **Reset the competition** to put everyone back to
  ten lakh.

## After a restart

If the laptop sleeps or the server restarts mid-session, the market comes back
**FROZEN** with the remaining time preserved. That is deliberate: the clock must
not run on while nobody could trade. Sign in as market operator and press
**Resume**.

## What this is not

This is a laptop on a Wi-Fi network. It is the right way to rehearse with twenty
people in a room and the wrong way to run a competition for five hundred.

For the real event you need a host that stays up on its own:
`docs/deploy-custom-domain.md` walks through Replit plus
`terminal.aaradhyjani.com`, adding one DNS record and leaving your email and
website untouched.
