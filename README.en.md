# GateLink HASS

[简体中文](README.md) | [English](README.en.md)

A Home Assistant custom integration for cloud access control services in China. Sign in with your phone number and an SMS verification code, select gates across residential communities, and get a separate device and **Open door** button for each bound gate.

Repository: [maybeLab/gate-link-hass](https://github.com/maybeLab/gate-link-hass). The integration domain is `gate_link_hass`, and its installation directory is `custom_components/gate_link_hass`.

## Unofficial project

GateLink HASS is an unofficial project developed independently by the community. It is not affiliated with, endorsed by, or developed in partnership with the access control service provider.

## Features

- Use multiple accounts, each with gates from multiple communities.
- List all authorized gates, including building entrances, custom gates, and community gates.
- Add gate buttons to dashboards, use them in automations, or expose them through HomeKit Bridge.
- Pause door opening and request reauthentication when a session expires, keeping existing gate bindings after sign-in.
- View a separate login status diagnostic entity for each account and a list of login checks in Home Assistant **Activity**.
- Change gate bindings in the configuration page; unbinding a gate removes its device and entity.

## Requirements

- Home Assistant Core **2026.10.0 or later**.
- An existing access control account that supports phone number and SMS sign-in, with permission to open the selected gates.
- Network access from Home Assistant to the cloud access control service. Opening a gate requires an internet connection.

## Authorized use and safety

- **Authorization**: Use only accounts you own or are authorized to use, and operate only gates you have permission to access. Do not use this project for unauthorized access or to bypass access controls. Follow applicable laws and the relevant service terms.
- **Cloud service dependency**: This project depends on third-party cloud services. Changes to endpoints, authentication rules, or service availability may prevent SMS delivery, sign-in, gate discovery, or door opening. Continued compatibility and service availability cannot be guaranteed.
- **Remote opening safety**: Before opening a gate remotely or configuring an opening automation, confirm that you have selected the intended gate and that conditions at the site are safe. Avoid accidental activation. A successful cloud response does not confirm that the gate physically opened, and this integration does not report its physical open or closed state.

The software is provided “AS IS” under the [MIT License](LICENSE). These notes describe specific usage boundaries and safety considerations; see the license text for the full terms and disclaimer.

## Installation

### HACS

The integration is currently available as a HACS custom repository and is not yet included in the default repository list. Use [Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=maybeLab&repository=gate-link-hass&category=integration), or add it manually:

1. Open **Custom repositories** in HACS.
2. Enter `https://github.com/maybeLab/gate-link-hass` and select **Integration** as the category.
3. Search for and download **GateLink HASS**.
4. Restart Home Assistant.

### Manual installation

1. Download the source from the [GitHub repository](https://github.com/maybeLab/gate-link-hass) and copy `custom_components/gate_link_hass` to `/config/custom_components/gate_link_hass` in your Home Assistant installation.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & services → Add integration** and search for **GateLink HASS**.
2. Enter your phone number and submit it to request an SMS verification code.
3. Enter the six-digit code. Select **Resend code** if needed.
4. Select at least one gate from the list, which shows each gate as “Community / Gate name”.
5. Set the request timeout and save.

The verification code is used only during sign-in and is not saved in the account configuration. Add each account once; repeat these steps to add other accounts.

You can change gate bindings and the request timeout in the integration's configuration page. Bound gates that temporarily disappear or lose authorization remain in the configuration and can be unbound. The server determines whether an opening request is authorized. Delete the entire configuration entry when you no longer need an account.

The top of each account's configuration page shows the masked phone number, user ID, current login check result, sign-in time, last successful check time, and the number of bound communities and gates. Times use your Home Assistant time zone. If a check temporarily fails, the page shows that the current status could not be confirmed and retains the last successful check time.

## Usage

Each gate has its own device and **Open door** button. By default, the button uses only the gate name, without the community name; you can rename it in Home Assistant. Add the button to a dashboard or call it from a script or automation:

~~~yaml
action: button.press
target:
  entity_id: button.gate_link_hass_entry
~~~

Replace the example entity ID with the actual button entity ID from the device page. The button sends an opening command; it does not report the gate's physical open or closed state.

Each press uses the saved session and gate binding and makes a single request to the opening endpoint. It does not check login status or fetch gates first. The opening response determines login and permission status.

Pressing the same button while a request is still pending reports that it is already being processed. Opening requests are not retried automatically. After a timeout, the outcome may be unknown; check the gate before deciding whether to retry.

### HomeKit

Select the gate buttons you want to expose in HomeKit Bridge's entity selection. In the Home app, each button appears as a switch that resets automatically.

## Login and permissions

Login status follows the cloud service's responses. Each account is checked daily at **06:00 Beijing time (UTC+8)**, regardless of your Home Assistant time zone. If the server returns an earlier reference time for the next check, a check is scheduled earlier. Reaching that time does not itself invalidate the session. Loading an account or opening its configuration page also checks login status and fetches gates. Pressing a gate button calls the opening endpoint directly.

When the server explicitly reports that a session has expired, door opening is paused and Home Assistant prompts you to reauthenticate. Sign in with the same account to restore existing bindings and device and entity identifiers.

Network errors, timeouts, and unexpected responses do not invalidate the session. Failed background login checks are retried after **5 minutes**. Expired gate authorization or insufficient permissions must be resolved with the access control service.

### Login status and activity

Go to **Settings → Devices & services → GateLink HASS → Devices** and select `GateLink HASS Account ****1234`, where the last four digits identify the account. Its **Login status** diagnostic entity belongs to the account device, separately from the gate devices. Each account has its own device.

| Login status | Meaning |
| --- | --- |
| Sign-in valid | The most recent login check succeeded. |
| Sign-in expired | The server confirmed that the session expired. Sign in again; gate buttons are paused. |
| Check failed | A network error, timeout, or unexpected response prevented confirmation. The session and last successful check time are retained, and you can still try opening a gate. |
| Not checked | No check result is available yet. |

Entity attributes include the latest check time, last successful check time, check source, and an error reason without sensitive details. The account device and diagnostic entity remain available even if the session has already expired at startup. Reauthentication restores the gate buttons.

Open **Activity** in the sidebar and filter by the account device or its **Login status** entity to see checks from startup, daily checks, retries, the configuration page, and reauthentication. Each actual check creates a record, including consecutive successful checks. If an opening response confirms an expired session, that result is also recorded without an additional login request.

You can also add an activity card:

~~~yaml
type: logbook
title: Account login checks
entities:
  - sensor.gate_link_hass_account_1234_login_status
hours_to_show: 168
~~~

Replace the example entity ID with the actual entity ID from the account device page. Home Assistant Recorder stores the history according to its retention and filtering settings. Enable Recorder and Activity, and ensure that neither this entity nor the check events are excluded. Activity records show only the last four phone number digits and check results, without verification codes or session credentials.

Account credentials are stored with the integration configuration in Home Assistant. Protect your configuration files and backups.

## FAQ

**No gates are available**

Confirm that the account has access to the community and its gates. Retry if a query fails. If a bound gate is temporarily unavailable, refresh the list in the configuration page or unbind it.

**Home Assistant asks me to sign in again**

Complete reauthentication under **Settings → Devices & services → GateLink HASS**. Restarting Home Assistant does not restore a session that the server has confirmed as expired.

**Reinstalling from a version with the old integration name**

Remove the old integration's account entries and installation directory first. Install the current version, sign in with an SMS code, and select your gates again. The internal domain has changed, and previous configuration is not migrated automatically. Update dashboards, automations, and HomeKit Bridge to use the new button entities.

## Documentation and contributions

The following documents are currently available in Chinese:

- [Development guide](docs/DEVELOPMENT.md): local testing, account flows, and extending the integration.
- [API documentation](docs/API.md): cloud service endpoints, requests, and responses used by the integration.
- [Adapter comparison](custom_components/gate_link_hass/adapters/README.md): differences between the two protocol implementations.

Report problems through [Issues](https://github.com/maybeLab/gate-link-hass/issues) or submit a [Pull Request](https://github.com/maybeLab/gate-link-hass/pulls). Include your Home Assistant and integration versions, and redact phone numbers, verification codes, and session credentials.

## Acknowledgments

Thanks to the following open-source projects and their authors for sharing research and implementations that informed this project's protocol adapters and flow validation:

- [Open-source project by shacha086](https://github.com/shacha086/QinlinClient)
- [Open-source project by 577fkj](https://github.com/577fkj/QinlinOpenDoor)
- [Open-source project by ship-it-th1](https://github.com/ship-it-th1/qinlin-remote-open)
- [Open-source project by rponeawa](https://github.com/rponeawa/QinlinOpen)

## License

This project is licensed under the [MIT License](LICENSE). Third-party code retains its respective license notices.
