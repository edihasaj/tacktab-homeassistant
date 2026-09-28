# Tacktab for Home Assistant

Control [Tacktab](https://applifyer.com/tacktab) wall tablets from Home Assistant.
Tacktab is a kiosk browser for Android and iOS that pins a tablet to one web page,
such as a Home Assistant dashboard. This integration turns the screen on and off,
changes the page, sets brightness and reports the battery.

Tacktab finds its way into Home Assistant by itself: with the remote control API
on, the tablet advertises itself on your network and Home Assistant offers to add
it. You only type the API password.

## What you get

| Entity | What it does |
| --- | --- |
| Screen (switch) | Off switches the display off, or blacks the screen out and dims it when Tacktab is not allowed to (see below). On wakes it. A tap on the tablet also wakes it. The `off_mode` attribute is `display` or `dim`. |
| Brightness (number) | Screen brightness, 0 to 100 %. Unknown means Tacktab follows the system brightness. |
| Page (text) | The page Tacktab shows. Set it to open another address. |
| Reload page (button) | Reloads the page. |
| Battery (sensor) | Battery level. |
| Charging (binary sensor) | Whether the tablet is charging. |
| App version (diagnostic sensor) | The Tacktab version on the tablet. |

The integration polls every 10 seconds. Each command's answer is the new state,
so the dashboard updates straight away.

## Requirements

- Tacktab 1.0.1 or later for discovery. Tacktab 1.0.0 works too, added by address.
- Tacktab 1.0.2 or later for a real display off on Android.
- The remote control API is a Tacktab Pro feature. Pro is a one-time purchase and
  has a free 24-hour trial.
- The tablet and Home Assistant on the same network.

## Set up Tacktab

1. In Tacktab, press and hold the top-left corner for 3 seconds to open Settings.
2. Under Remote control, select Generate, then Copy and Save. Keep the password
   for Home Assistant. Tacktab needs at least 8 characters; a generated one is 20.
3. Turn on Remote control API. Tacktab shows the tablet's address underneath.
4. Android only, optional: next to Screen off, select Allow. Without it, "off"
   dims the screen to black instead of switching the display off. A tablet set
   up as device owner (Tacktab's full lock) can already switch it off.

## Install

### HACS

1. HACS, three-dot menu, Custom repositories. Add
   `https://github.com/edihasaj/tacktab-homeassistant` as an Integration.
2. Install Tacktab, then restart Home Assistant.

### Manually

Copy `custom_components/tacktab` into your Home Assistant `config/custom_components/`
folder and restart.

## Add a tablet

Home Assistant shows "Discovered: Tacktab" under Settings, Devices & services.
Select Add and enter the API password. To add one by hand, choose Add integration,
Tacktab, and enter the address and password from Tacktab's settings.

If you change the password in Tacktab, Home Assistant asks for the new one.

## Examples

Sleep the kitchen panel at night and wake it in the morning:

```yaml
automation:
  - alias: Kitchen panel sleeps at night
    triggers:
      - trigger: time
        at: "23:30:00"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.kitchen_tablet_screen

  - alias: Kitchen panel wakes up
    triggers:
      - trigger: time
        at: "06:30:00"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.kitchen_tablet_screen
```

Show the camera page when the doorbell rings:

```yaml
automation:
  - alias: Doorbell shows the camera
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.hallway_tablet_screen
      - action: text.set_value
        target:
          entity_id: text.hallway_tablet_page
        data:
          value: http://homeassistant.local:8123/dashboard-door/camera
```

## The API

The integration uses Tacktab's local HTTP API on port 7979, documented at
https://applifyer.com/tacktab/api. You can call it from anything else too.

## Security

- Every call needs the password as `Authorization: Bearer <password>`. A password
  in the address (`?password=`) is refused.
- After 5 wrong passwords from one address, Tacktab refuses that address for 30
  seconds, doubling up to 15 minutes. Home Assistant then shows "Too many wrong
  passwords". Other addresses are not affected.
- Requests from a web page (they carry an `Origin` header) are refused, so a
  website open on another device cannot control the tablet.
- The API is plain HTTP on your local network. Keep it there: do not forward port
  7979 on your router.

## Development

```sh
uv sync --group test
uv run pytest
```

## License

MIT. Tacktab itself is a separate app by Applifyer, LLC.
