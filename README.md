# SingleMall for Home Assistant

Your SingleMall shopping list in Home Assistant, as a to-do list. Sign in
once with the account you use on your phone; after that Home Assistant can
add an item in words, change the words of an item you typed, write or clear
a note, tick and untick, and remove items. Each change reaches your phones,
and your group's phones, the same way a change made in the app does. A change
made on a phone shows up in Home Assistant within about 30 seconds.

**It is for people already allowed to use SingleMall.** Anybody else can
install it, but the sign-in refuses their account.

Only the shopping list is available here. Favourites, the meal plan and
prices are not.

## Installing with HACS

1. In Home Assistant open HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/MicheledG/singlemall-home-assistant` with type
   **Integration**.
3. Find **SingleMall** in HACS, download it, and restart Home Assistant.
4. Go to Settings → Devices & services → **Add integration** → SingleMall,
   and sign in.

When a new version is out, HACS shows it as an update: install it and
restart.

## What it adds

| Thing | Value |
|---|---|
| Entity | `todo.singlemall_shopping_list`, the same whatever language Home Assistant uses. Its name is "Shopping list" in English and "Spesa" in Italian. |
| Item summary | The line's name alone, so Home Assistant's own list sentences match items by name. |
| Item description | The note. When the quantity is above 1 it starts with `×N`, for example `×2 — semi-skimmed`. Editing the `×N` changes the quantity; a description without it means a quantity of 1. |
| Item status | Completed when the line is ticked. |

Adding words that are already on the list raises that line's quantity
instead of adding a second line. New words for an item you typed take its
place, keeping its quantity, tick and note. An item picked from the
catalogue keeps its product's name, so renaming it is refused with a
message.

## Signing in

The only sign-in callback the app client allows is
`https://my.home-assistant.io/redirect/oauth`. The first time, the
my.home-assistant.io page asks for your Home Assistant's address and
remembers it in that browser. It then sends you back to your own Home
Assistant to finish. Nothing has to be reachable from the internet. The
browser does the round trip.

Sign in with the account you use on the phone. An account that is not
allowed to use SingleMall is refused at sign-in. The sign-in lasts a
year (the refresh token's lifetime). After that, or if the account is
removed from SingleMall, Home Assistant asks you to sign in again
(Settings → Devices & services) instead of failing silently.

Removing the integration revokes its sign-in, so a copy of it left in a
Home Assistant backup cannot be used. This is best effort: if the sign-in
service cannot be reached at that moment, the removal goes ahead and the
sign-in expires on its own within the year. An access token issued
before the removal keeps working until it expires, at most one hour
later, because the API checks tokens without asking the sign-in service.
