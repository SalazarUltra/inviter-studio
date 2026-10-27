"""English edition of ``ui.help_content.GUIDES``.

Every guide keeps the same keys and the same number/order of blocks as the Russian one, so the help dialog can
switch language without losing its place. A guide missing here (or a block list that is empty) falls back to the
Russian text via ``ui.help_content.get_guides``.

Parts of the interface are still shown in Russian, so button names that are not translated yet are given as
English meaning followed by the Russian label in «guillemets». The text templates ``{имя}`` and ``{привет|...}``
are literal Russian syntax understood by the sender and are kept as is.
"""

GUIDES_EN = {
    "start": {
        "title": "Getting started",
        "intro": "First run: connect an account, add recipients and set up a task.",
        "blocks": [
            (
                "1. Connect your Telegram",
                "Open Accounts → “+ Add account”. Enter the phone, API ID and API Hash of your own Telegram app. "
                "Get the code and confirm the login; if Telegram asks for a two-step verification password, enter it "
                "separately.",
            ),
            (
                "2. Choose the action and the campaign",
                "A campaign is a separate recipient list with its own settings and results. For a new list press "
                "“+ Campaign” («+ Кампания»). “Direct messages” sends the text you enter to people. “Invites” adds "
                "people to the selected group or channel: the account needs admin rights with the invite permission. "
                "The campaign name is arbitrary; the action defines its mode.",
            ),
            (
                "Delete a campaign",
                "In Task or Audience select the campaign and press “Delete” («Удалить») next to “Report” («Отчёт»), or "
                "open the “⋯” menu → delete this campaign. Stop the task first. The confirmation deletes its list, "
                "settings, log, reports and unsaved changes; waiting second messages are cancelled. If it was the last "
                "campaign, a new empty one is created. Accounts, AI connections, parsing and other campaigns stay. The "
                "internal protection against repeated sends stays too. To undo a deletion you need a database copy made "
                "beforehand.",
            ),
            (
                "3. Paste the list of people",
                "In Task paste people into the “Send to · recipient list” field: one username per line, for example "
                "@alice and @bobby. Press “Save list” («Сохранить список»). The same list can be edited in Audience. "
                "Empty lines are skipped, duplicates are merged. Add recipients who are expecting your message.",
            ),
            (
                "4. Set up and check the task",
                "In Task enter the text for direct messages or the chat address for invites. Choose accounts and the "
                "interval. “Preview” checks the data without connecting to Telegram. “Check connection” checks "
                "sessions and the connection without sending messages or invites.",
            ),
            (
                "5. Start and follow the results",
                "Only “Start task” begins sending or inviting. Keep the program open. See each recipient's result in "
                "Audience, events in Log, and run totals in Report. “Pause” can be resumed, “Stop” ends the current task.",
            ),
            (
                "What's new: queue, A/B, conversion, language",
                "Campaign queue: when a task ends, the app offers to start the next campaign (optionally on its own). "
                "The A/B test in Task adds a second variant of the first message with a set share. "
                "A campaign can be exported and imported as JSON (“⋯” menu). "
                "The Overview shows conversion by campaign and period; results can be sent to Saved Messages. "
                "The interface language (RU/EN) is switched in the side menu without a restart; a wizard helps on the "
                "first run. Parts of the interface are still being translated, so some buttons may appear in Russian.",
            ),
            (
                "Support the project",
                "The support window appears once on the first run of this profile. You can close it without donating. "
                "Open it again with the “Support the project” button on the overview or the heart in the side menu. "
                "The window shows a wallet address; the button copies it in full. "
                "For Ethereum, USDT (ERC-20) and ETH are listed; for BNB Smart Chain — USDT (BEP-20) and BNB. "
                "Choose the network you need and make the transfer yourself in your wallet.",
            ),
            (
                "After closing",
                "Saved accounts, lists, results and settings stay. The direct-message text is saved automatically; other "
                "task parameters are saved with “Save settings”. A task never starts by itself after opening. AI and "
                "warm-up are not required to send a ready text.",
            ),
        ],
    },
    "accounts": {
        "title": "Accounts: connecting Telegram",
        "intro": "This is where the Telegram accounts the program works as are kept.",
        "blocks": [
            (
                "Phone, API ID and API Hash",
                "Enter the phone with the country code, for example +380… . API ID is a number, API Hash is a "
                "32-character string: both are issued in API development tools at my.telegram.org. They are the "
                "credentials of your own Telegram app. The login code arrives through Telegram; the 2FA password is "
                "needed only if two-step verification is on.",
            ),
            (
                "Where to get API ID and API Hash — step by step",
                "In the add form press “Get API ID and API Hash”: my.telegram.org/apps opens. Log in with your number; "
                "the code for the site arrives in Telegram. Open API development tools. If you have no app yet, set a "
                "title, a Short name in Latin letters and digits, Platform — Desktop, a short description and press "
                "Create application. Copy App api_id into API ID and App api_hash into API Hash. If an app already "
                "exists, use the values shown there. Then press “Get code” in our program: a new code arrives for this "
                "login. The “Step by step: where to get them” button opens detailed instructions right in the form.",
            ),
            (
                "Proxy — an optional field",
                "If you have no proxy of your own, leave the block empty. For your own proxy enter SOCKS5 or HTTP, the "
                "server address, the port and, if needed, a login/password. “Check” verifies the saved account and the "
                "connection without sending anything.",
            ),
            (
                "VPN and an AmneziaVPN key",
                "Paste a vpn:// key into the AmneziaVPN app on your computer and connect the VPN there. The proxy field "
                "in Inviter Studio accepts a SOCKS5/HTTP address, not a VPN key. On Windows a connected VPN normally "
                "affects the connections of the whole program; exceptions and split routing are set in AmneziaVPN. A "
                "separate VPN key per account inside one copy of the program is not supported. A VPN does not confirm "
                "the right to send messages and does not remove Telegram's refusals.",
            ),
            (
                "What “In use” means",
                "An enabled account is available for selection in the task. Additionally tick the accounts you need in "
                "the task itself. Turning it off excludes the account from further selection but does not delete its "
                "data.",
            ),
            (
                "Daily limit and counters",
                "The limit sets the maximum of first direct messages and, separately, invites for each account. The "
                "second direct message after a reply is counted separately and does not use up the first-message limit. "
                "Zero forbids first messages of that mode. The really available limit may be lower than the set one: it "
                "is capped by the account age (the field in the card or the date added) — up to 10 a day in the first 7 "
                "days, up to 20 until day 30, after that your limit applies. Daily counters are kept between runs; a "
                "limit does not guarantee the absence of Telegram restrictions.",
            ),
            (
                "Edit and delete",
                "“Edit” saves the limit, proxy and account state. Deleting removes the account and its local session "
                "from the program; adding it again requires a login. Stop the tasks that use it first.",
            ),
            (
                "Connection closed — account kept",
                "After a task stops or finishes, the program closes the temporary network connection and writes it to "
                "the log. The account and the saved login stay in the program. Deleting an account needs the separate "
                "“Delete” button with a confirmation.",
            ),
        ],
    },
    "targets": {
        "title": "Audience: the recipient list",
        "intro": "All accounts selected for the task share the queue of the selected campaign.",
        "blocks": [
            (
                "Paste, edit and clear",
                "“Paste / edit list” («Вставить / изменить список») opens all waiting recipients, regardless of the "
                "filter and page. Paste with Ctrl+V, the right mouse button or “Paste”. Format: one username, "
                "@username or public t.me link per line. Fix and delete lines, then press “Save list”.",
            ),
            (
                "How saving works",
                "The editor replaces only the waiting queue. “Clear field” → “Save list” deletes the waiting recipients "
                "after confirmation. Processed results and the protection against repeated messages stay. “Cancel” "
                "closes without applying edits. Edit after the task has stopped.",
            ),
            (
                "Import, shuffle and export",
                "TXT/CSV import adds people to the existing queue, without duplicates inside the campaign. A line may "
                "be an @username, a t.me link, a number or id:123456789 (a person without a username, for chat invites "
                "only; the account must already “know” them in Telegram). Shuffle changes the order of waiting "
                "recipients. CSV export saves the whole audience with results, including rows outside the open page and "
                "filter. Search and filter change only what is shown.",
            ),
            (
                "“Retry errors”",
                "A choice of statuses opens. By default “Error” (and “Check” in invites); “Private” and “Not found” "
                "only if you tick them. In direct messages the “Check” status is not returned by a retry: mark “Sent” / "
                "“Not sent” manually. The protection by the delivery log stays.",
            ),
            (
                "“Message private ones with a link”",
                "Takes recipients with the “Private” status (the invite was refused because of privacy) and creates a "
                "new “Direct messages” campaign with the text and a link to the chat. Nothing is sent by itself: check "
                "the text and interval and press “Start task”. People already written to per the delivery log do not "
                "enter the queue.",
            ),
            (
                "“Clear all”",
                "Deletes all audience rows of this campaign, including results. Campaign settings, the action log and "
                "the separate protection against repeated direct messages stay. To remove only people not processed "
                "yet, use clearing in the editor.",
            ),
        ],
    },
    "task": {
        "title": "Task: sending and invites",
        "intro": "First choose the campaign and the mode. Settings apply when the task starts.",
        "blocks": [
            (
                "Four setup steps",
                "1. Recipients: choose the mode, paste @usernames and press “Save list” (or import from parsing). "
                "2. Message / chat: the text for direct messages or the group @address. 3. Accounts, interval and queue "
                "order; “Schedule and breaks” holds extra parameters. 4. Review → “Start task”. “Next”/“Back” only "
                "switch screens.",
            ),
            (
                "Short invite checklist",
                "1) A chat and accounts with the invite right. 2) Order: as in the list or shuffled. 3) On start "
                "accounts rotate; the status shows successes, skips and the nearest free account. 4) One account "
                "without rights or on a Telegram pause does not stop the others. 5) “Retry errors” — tick the statuses "
                "you need (“Private” and “Not found” are not returned by default). 6) Private ones → “Message private "
                "ones with a link” in Audience (direct messages with a chat link; start manually).",
            ),
            (
                "Short direct-message checklist",
                "1) “Direct messages” mode; default interval 60–120 s (not below 45). "
                "2) A text with `{имя}` / `{a|b}`; preview or “Test to Saved Messages”. "
                "3) Optionally a second message after a reply (30–180 s and “Wait for reply, hours”). "
                "4) Accounts and schedule; see the time estimate in the review. "
                "5) “Start” and keep the program open. "
                "6) “Stop” with waiting second messages asks for confirmation. "
                "7) In Audience, for “Check” choose “Sent” / “Not sent”; “Retry errors” does not return uncertain "
                "automatically.",
            ),
            (
                "Windows notifications",
                "The switch at the bottom of the window turns on notifications about task completion, parsing results "
                "and errors of its operations. The choice is saved for the whole program. Message texts, phones and "
                "recipients never get into notifications. Windows may hide them in Do Not Disturb mode. Results stay in "
                "the program: for tasks — the log and the report, for parsing — the results screen and the source "
                "report. Tasks do not continue while the program is closed.",
            ),
            (
                "Text templates",
                "In direct messages press “Templates…”. Save a text under a clear name, then use it in any campaign. "
                "“Use text” inserts the content into the task; the current non-empty text is replaced after "
                "confirmation. Sending does not start. Templates contain plain text without placeholders and are stored "
                "in the database.",
            ),
            (
                "Direct messages or invites",
                "“Direct messages” sends the entered text to each recipient in a private chat. To send a link to your "
                "chat, put it into the message text. “Invites” adds people straight into a public group or channel by "
                "@address; the selected accounts need admin rights with the invite permission. A private link such as "
                "t.me/+… does not fit this field.",
            ),
            (
                "Where the list of people is",
                "Task itself has the “Send to · recipient list” field. Paste @usernames one per line with Ctrl+V or "
                "“Paste”, then press “Save list”. The field can be edited and cleared; clearing the queue needs saving "
                "and confirmation. “Refresh” reloads the queue and lets you discard field changes. Only waiting "
                "recipients are edited; processed results stay in Audience of the same campaign. When leaving or "
                "switching campaigns, unsaved changes need confirmation; starting with an unsaved list is blocked.",
            ),
            (
                "Text and accounts",
                "The direct-message text keeps line breaks. {имя} inserts the recipient's name (“друг” if the name is "
                "unknown), {привет|здравствуйте|добрый день} picks one option at random for each send; other curly "
                "braces stay as they are. An emoji may take two characters of the 4096 limit. The text is saved "
                "automatically. Select at least one available account. In direct messages accounts work in turn; the "
                "shared log by username and Telegram ID prevents repeated sends across accounts and campaigns.",
            ),
            (
                "Second message only after a reply",
                "In “Direct messages” mode fill in the first message, turn on “Second message — only after a reply” and "
                "paste the second text. "
                "The second one is sent once, from the same account, after that person's new message in the private "
                "chat. First and second messages share one daily account limit (the safe limit that accounts for age). "
                "A text, sticker or other normal message counts as a reply; old messages, reactions, read marks and "
                "service records do not. "
                "After a reply the second message does not go at once: after a random 30–180 seconds and not earlier "
                "than the minimum interval after the last send. "
                "“Wait for reply, hours” (1–168, default 24) — if no reply comes for longer, the wait is cancelled as "
                "“Reply wait expired”. "
                "A Telegram restriction pauses only the affected account; the campaign does not stop. "
                "Without a reply there is no second message. While paused, outside the schedule or with an exhausted "
                "quota, sending waits. "
                "After the first queue the task keeps waiting for replies: keep the program open. “Stop” (with a "
                "confirmation if some are waiting) ends observation and sending and cancels second messages not sent "
                "yet; first sends and the protection against repeats stay. "
                "The program must be open to deliver, but waits are not reset by a restart: closing or restarting the "
                "program keeps the waiting second messages, and the next start of the same campaign with the mode on "
                "continues them (the “Wait for reply, hours” period runs while the program is closed too). "
                "{имя} in the second message comes from the name saved at the first message; without a name it is "
                "“друг”. "
                "Saved replies are checked gradually through chat history: per cycle a few people per account with "
                "pauses. Audience has notes, Log has second-message events. "
                "“Cancel waiting second messages” cancels the waits if they remain after a failed finish. Clearing the "
                "audience does not cancel them by itself. "
                "Changing the second text applies to new first messages: old waits keep their text. "
                "Turning the mode off keeps waits stored at the next start but does not send second messages. "
                "Telegram does not reveal every block. A known ban ends the attempts; a hidden block after a reply "
                "cannot be reliably detected in advance. "
                "After an unknown send result there is no automatic retry — check the chat manually.",
            ),
            (
                "Interval in seconds",
                "The program picks a pause between the minimum and the maximum. For direct messages the default is "
                "60–120 s, minimum 45 s (lower risks PeerFlood). For an equal pause set equal numbers: 60 and 60 is "
                "about one first message a minute for the whole queue. Second messages after a reply wait 30–180 s and "
                "the minimum interval after the last send; first and second share one daily account limit. Accounts do "
                "not send in parallel. In invite mode the pause may grow at night and late in the evening (smart "
                "interval by the PC system clock). 45–86400 s is allowed for messages and 10–300 for invites.",
            ),
            (
                "Pace: Safe / Normal / Fast",
                "The buttons in the interval block fill in the interval, the number of actions before a break and the "
                "break length. Direct messages: Safe 90–180 s / break after 3; Normal 60–120 / 5; Fast 45–90 / 7. "
                "Invites: Safe 40–90 / 3; Normal 20–45 / 5; Fast 15–30 / 8. If the numbers are changed by hand, "
                "“Custom” is shown. Before the start the program warns and asks for confirmation if the message "
                "interval is below 60 s, the invite interval below 20 s or the break comes only after 9+ actions. This "
                "lowers the risk of restrictions but does not guarantee protection against them.",
            ),
            (
                "Campaign daily limit and warmed-up accounts",
                "“Campaign daily limit” (0 — off) caps the number of successful first actions per calendar day by PC "
                "time: people added or first messages. After reaching the limit the task waits for the next day (second "
                "messages after a reply keep working). The “Run only with warmed-up accounts” mark skips accounts whose "
                "warm-up is not finished; on start it shows which accounts were skipped.",
            ),
            (
                "Schedule and breaks",
                "The schedule allows actions only between the given hours by this computer's time; the range may cross "
                "midnight. Weekdays (Mon–Sun) can be ticked separately: on other days the task waits, as outside the "
                "hours; all are selected by default. For invites the “smart interval” takes the time of day from this "
                "computer's clock or from Moscow (Europe/Moscow) — the “Count smart interval by” choice. The program "
                "must be running and the task started. After the set number of successful actions the account takes a "
                "break in minutes; other available accounts may continue the queue.",
            ),
            (
                "Save, check, start",
                "“Save settings” saves the parameters of the selected campaign. “Preview” checks filling and the queue "
                "without connecting and shows the time estimate. “Check connection” contacts Telegram without sending. "
                "“Start task” saves the settings, checks the connection and begins real actions.",
            ),
            (
                "Pause, stop and closing",
                "“Pause” suspends the next actions; “Resume” continues. “Stop” ends the task, waiting recipients stay in "
                "the queue. A request already sent may still finish. Closing the program stops the work; at the next "
                "start you must turn continuation on manually.",
            ),
        ],
    },
    "warmup": {
        "title": "Warm-up: actions in selected chats",
        "intro": "A day-by-day action plan. You can warm several accounts in parallel — each keeps its own chats.",
        "blocks": [
            (
                "What warm-up does",
                "The plan may include being online, reading, views, reactions and messages in selected chats. It does "
                "not collect new recipients and does not guarantee protection against Telegram restrictions. Warm-up is "
                "not required to send a ready text from Task.",
            ),
            (
                "How to start and several accounts",
                "Choose an account (name and @username appear in the list) and a level of 3, 7 or 14 days. Refresh chats, "
                "tick the ones you need, set a topic and press “Start warm-up”. To warm another account: switch it in the "
                "list, tick its chats and press Start again. On the right: “Active warm-ups” and “Stop all”. The same "
                "account cannot be in warm-up and a task/parsing at once. Leave the program open.",
            ),
            (
                "AI, tone and model prompt",
                "With an active AI connection the account writes like a chat member. Tone (neutral / friendly / expert) "
                "changes writing style; save AI parameters. The scenario block on the right shows the AI request text "
                "(chat-agnostic; topic is a soft hint, live conversation comes first). Reply probability, hourly limit "
                "and chat pause cap frequency. Each account has its own memory per chat. Clearing memory does not lift "
                "send limits.",
            ),
            (
                "Channels and stopping",
                "In broadcast channels the program does not post. Chat rights and Telegram limits may block actions. "
                "Status and plan day are for the selected account; other active warm-ups keep running. “Stop” ends the "
                "selected run (or all if the selected one is idle). Warm-up does not resume by itself after closing.",
            ),
        ],
    },
    "ai": {
        "title": "AI Studio: text generation",
        "intro": "Connecting a model for warm-up messages. A normal broadcast sends your ready text.",
        "blocks": [
            (
                "How to connect",
                "Choose OpenAI, Anthropic (Claude), Google Gemini or xAI (Grok). “Get key” opens the official console; "
                "steps for the selected provider are shown beside it. Create an API key, paste it with the button or "
                "Ctrl+V, choose a model and give it a name. “Save connection” saves the settings without calling AI. "
                "Then check the example and press “Activate” on the right. A normal broadcast does not need AI.",
            ),
            (
                "Which model to choose",
                "The first model in the list is a starting option for short messages. The selection of text models was "
                "checked against the official documentation on 2026-10-15; the “Models” button opens the provider's "
                "current catalog. You can type another ID by hand. The list does not guarantee access: it depends on the "
                "API account, region and quota. A model's API ID is its name, not a Telegram API ID.",
            ),
            (
                "Change the key or model",
                "Press “Edit” on a saved connection: the fields on the left fill in for editing, the key stays hidden. "
                "Save the changes or cancel editing. The token counter and the selected active connection are kept. When "
                "the provider changes in the form, the previous key is cleared. A warm-up that is already running must "
                "be restarted to apply the changes.",
            ),
            (
                "Check AI / show an example",
                "Enter a topic: the program makes one real request to the selected provider and shows the answer. This "
                "example is not sent to Telegram. The request may be paid; see the rate and available quota in the "
                "provider's console.",
            ),
            (
                "Tokens and cost",
                "Tokens are units of text counted by the AI provider, not Telegram message counts. Cards and the test "
                "preview show an estimated request cost and total USD (from public rates or the provider’s billed cost "
                "when available). Always confirm exact charges in the provider console: short replies can still use many "
                "tokens because of the system prompt and reasoning. Enter the API key only in its field. HTTP 401 — key, "
                "403 — rights, 404 — model/API, 429 — quota or rate.",
            ),
        ],
    },
    "logs": {
        "title": "Log: events and results",
        "intro": "Events of the selected campaign. The result for each person is in Audience.",
        "blocks": [
            (
                "Search and filters",
                "Enter a username, phone or part of an event. Choose successful actions, skips, or errors and waiting. "
                "“Refresh” rereads the saved events of the selected campaign. Search works on the last 2000 loaded "
                "records; export saves the whole log regardless of the filter. The log text itself can be read and "
                "copied.",
            ),
            (
                "What to look at here",
                "The log shows saved events: sends, list changes, connections and other actions. A “working connection "
                "closed” entry means the program closed the active connection after a task; the saved session and the "
                "account stay in Accounts. A separate notification appears when Telegram asks for a pause or a new login "
                "is needed. After a refresh the log loads from the database; not every temporary notification is saved. "
                "Check each recipient's status and the reason for a skip in Audience.",
            ),
            (
                "Refresh and export",
                "Saved events load when you go to Log. The screen shows the last 2000 lines. “Export log” saves all "
                "recorded events of the selected campaign to TXT or an Excel table (XLSX). To view another campaign, "
                "pick it in the panel above the log.",
            ),
            (
                "Clear and report",
                "“Clear log” deletes the saved events of the selected campaign. Recipients, results and the protection "
                "against repeated messages stay. “Report” in the campaign panel shows results by account and the last "
                "100 runs with durations.",
            ),
        ],
    },
    "parsing": {
        "title": "Parsing: members and message authors",
        "intro": "Choose an available list of members or of people who wrote in available history. Results are saved "
        "separately from campaign audiences.",
        "blocks": [
            (
                "1. Choose the account and sources",
                "Open Parsing, pick an account (name and @username show in the list) or turn on “Several accounts”. "
                "Paste public @addresses or https://t.me/name links, one per line. In multi mode “My groups” merges "
                "chats from every ticked account and shows which account owns each chat; select a group and press "
                "“Add”. t.me/+… invite links do not fit here: join manually, then refresh “My groups”. “Paste” and "
                "“Clear field” change only the sources field.",
            ),
            (
                "2. When joining is needed",
                "In a public group you can check access without joining. If Telegram lets the selected account view "
                "members, the list works that way too. For a private group first join manually by a valid link or get an "
                "invitation and approval of the request if one is needed. An ordinary group is available to its "
                "members. The program never joins anywhere by itself.",
            ),
            (
                "Channels and hidden members",
                "A public channel or a subscription to it does not guarantee access to the subscriber list: Telegram "
                "usually requires admin rights. In a group an admin can hide members from ordinary users. The program "
                "reports the restriction and does not try to bypass it. A channel's discussion group is a separate "
                "source; its members are not all subscribers of the channel.",
            ),
            (
                "“Message authors” mode",
                "In the “What to get” field choose “Message authors”. This mode reads only the history available to the "
                "account, without requesting the member list. It shows people on whose behalf ordinary messages are "
                "visible, including media and forwards. A found author may have already left the group; their current "
                "role and membership are not checked. Anonymous messages and posts on behalf of channels do not reveal a "
                "specific person. Look for channel comments in its discussion group — it is selected separately.",
            ),
            (
                "Period and message limit",
                "Choose 7, 30 or 90 days, the whole available history or “Custom dates”. Give both custom dates as "
                "YYYY-MM-DD; the last date is included fully by computer time. A limit of 1 to 10000 means the number of "
                "viewed messages per source, not people. The overall cap is 20000 messages per run. If the limit ran "
                "out before the period began, the result is marked as part of the history. “Check access” checks the "
                "history of every source in the list, without sending or joining; if the account itself hit a Telegram "
                "restriction, the other sources are marked as not checked. A private chat needs the account's access; "
                "old history may be hidden even from members.",
            ),
            (
                "Counters and author filters",
                "For an author the number of found messages and the dates of the first and last found message are "
                "saved. These are figures of only the viewed part of the history. Choose “From 5 messages”, “Wrote in 7 "
                "days” and grouping by message count. Counters add up across the selected sources; once one group is "
                "chosen they refer only to it. A message date differs from the online status. The role filter is off "
                "for authors: we do not determine admins from history. If Telegram returned only the author's ID, clear "
                "“With username” to see them; without a username they will not reach TXT or the audience.",
            ),
            (
                "3. Check and get members",
                "“Check access” checks all sources and writes OK/FAIL to the report. Set a limit of 1 to 5000 members "
                "per source and press “Get members”. Up to 10 sources per run, overall cap 20000 records. Large groups "
                "are filled in by searching letters if the “recent” members ran out earlier. Parsing can run while a "
                "task or warm-up runs on other accounts. “Several accounts” (members and message authors) divides the "
                "sources among the ticked accounts in turn; if one hit a Telegram pause, the others take its remaining "
                "sources. On a Telegram pause press “Continue” — unfinished sources are completed without duplicates.",
            ),
            (
                "4. Filters",
                "By default: with a username, without bots, deleted, scam, fake and restricted accounts. There are "
                "filters by group, role, photo and Premium. Search by name/username/ID does not lag on large lists. "
                "Filters work on saved data and do not contact Telegram.",
            ),
            (
                "What activity means",
                "Online is the visible status at the moment of fetching. “Recently”, “within a week”, “within a month” "
                "are approximate Telegram categories. “Exact time available” means Telegram showed the last-seen time. "
                "The “Last 7 days: exact time” filter uses only such time. “Recently / within a week” also includes a "
                "visible exact time within seven days. “Not visible” means no data, not inactivity. Photo and Premium "
                "are also shown only from the fetched profile.",
            ),
            (
                "5. Grouping and duplicates",
                "Grouping orders people and shows numbers by source groups, roles, activity, username presence or "
                "Premium. One Telegram ID is one row, even if the person is in several sources. All their source groups "
                "are kept; the sum over groups or roles may exceed the number of unique people. Groups with equal names "
                "are told apart by ID.",
            ),
            (
                "6. How to export",
                "After parsing, “Results and saving” opens. Choose a run, filters if needed, or the “Source report”. "
                "“New since the last run” shows, exports and passes to the audience only people whose Telegram ID was "
                "not in the selected earlier run. Formats: XLSX (convenient for Excel), CSV, JSON, TXT (@usernames), TXT "
                "with IDs (id:123456789 for people without a username). “Save file to PC” → folder and name in the "
                "Windows dialog. Optionally “To bot (TXT)” sends the same filtered segment as a TXT file to your bot "
                "from Accounts → My bot… (token required; nothing is sent automatically). The whole filtered segment "
                "is exported. Texts and phones are not saved.",
            ),
            (
                "7. Pass to the audience",
                "Choose a campaign and the order (as parsed / by activity / shuffled). Optionally exclude people already "
                "in the target chat. “To audience…” adds people with a username (with the “Including people without a "
                "username (by ID)” tick — also those whose ID alone is known; they suit only invites); admins and "
                "creators are skipped until you turn on “Including admins”. People already invited to this chat (global "
                "log) are skipped too. The task does not start by itself.",
            ),
            (
                "Short parsing checklist",
                "1) Account and sources → “Check access”. 2) “Get members” / authors. 3) If interrupted — “Continue”. "
                "4) Filters (scam/admins). 5) Exclude target-chat members if needed. 6) “To audience…” or save XLSX. "
                "7) In Task start invites or messages.",
            ),
            (
                "Saving, stopping and completeness",
                "The last 50 runs are in the results menu. “Stop” keeps a part. An interrupted run can be continued with "
                "“Continue”; a full repeat with the same sources creates a new result. Telegram may return an incomplete "
                "list. “Delete this run” deletes only its result.",
            ),
        ],
    },
    "statuses": {
        "title": "Statuses and errors",
        "intro": "Why a recipient was skipped and when a manual check is needed.",
        "blocks": [
            (
                "Waiting / In progress",
                "“Waiting” — the person is still in the queue. “In progress” — the program is processing them now. The "
                "display may update with a small delay.",
            ),
            (
                "Sent / Added",
                "“Sent” — Telegram confirmed the message request was sent. It does not mean the person read it. "
                "“Added” — the invite completed successfully.",
            ),
            (
                "Private / Not found / Already in chat",
                "“Private” — Telegram forbade the action, for example because of privacy. In invites such people can be "
                "moved to direct messages with the “Message private ones with a link” button. In message mode a private "
                "one is skipped and the queue goes on. “Not found” — the username could not be resolved; marked as a "
                "skip. “Already in chat” — no need to invite again.",
            ),
            (
                "No repeat",
                "The recipient is already in the shared direct-message log: the send was confirmed, forbidden or its "
                "result is unknown. Another account, a new campaign or clearing the list does not lead to an automatic "
                "repeat send.",
            ),
            (
                "Check",
                "The connection broke or the request was cancelled — it is impossible to say confidently whether the "
                "message went. The campaign stops only on such an unknown send result. In Audience mark manually “Sent” "
                "or “Not sent”. There is no automatic retry.",
            ),
            (
                "Error / Telegram wait",
                "See the reason in the log. “Not found” and a refusal for one person do not stop the queue. A Telegram "
                "pause or PeerFlood pauses only the affected account; the others continue. The campaign stops if no "
                "available accounts remain or the send result is unknown (“Check”). The account's “Check” button "
                "confirms login, but not the right to write to every new recipient.",
            ),
        ],
    },
    "data": {
        "title": "Saving and backups",
        "intro": "How to close the program and then return to your data.",
        "blocks": [
            (
                "What is saved",
                "Added accounts, the saved audience, results, the log and saved settings remain after exit. The "
                "direct-message text is saved automatically; task parameters with “Save settings”. In the audience "
                "editor press “Save list”. The login code and unfinished account forms are not saved as a draft.",
            ),
            (
                "What happens on exit",
                "The application ends background actions and connections. While it is closed, the schedule and sends do "
                "not work. On reopening, the selected campaign is restored; the task does not start by itself.",
            ),
            (
                "Where your data is",
                "In the portable version (the ZIP from GitHub) all data is stored in the profile folder next to "
                "start.bat. It is created after the first run and holds the database, settings and Telegram sessions. "
                "Never publish this folder, put it on GitHub or send it to other people — it holds access to your "
                "accounts. The ZIP release has no profile folder: every user has their own empty profile.",
            ),
            (
                "Why different launches may show different data",
                "Running from source uses the data and sessions folders in the project directory. Portable writes to "
                "profile next to the program. Use the same way of launching; move the profile only while the program is "
                "closed.",
            ),
            (
                "Installation and update",
                "Download InviterStudioPortable.zip from the official release, unpack it completely and run start.bat. "
                "Python is already inside. To update, close the program and run update.bat — it checks SHA-256 and does "
                "not touch profile. An AI key is needed only for AI features.",
            ),
            (
                "Data copies",
                "The “Data copies” button saves the database: accounts, campaigns, settings and history. Telegram "
                "session files are not part of this copy. Do not publish the .db file or send it to outsiders. For a "
                "full portable backup, with the program closed, copy the whole profile folder. Automatic copies are made "
                "at startup, the last seven are kept.",
            ),
            (
                "Restore",
                "The selected copy is applied at the next start; the current database is saved first. Later actions are "
                "absent from the old copy, including the send log. So restoring an old database may remove information "
                "that prevents repeats. Encrypted keys on Windows are available to the original user; on another "
                "computer a new login may be needed.",
            ),
        ],
    },
}
