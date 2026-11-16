# Explicit device profiles for the distributable adapter

## Intent

Issue #2 completes the native profile migration gate. A fresh installation must
not select the pilot desk through embedded light, area or channel identities.
Existing explicit profiles retain their resource identities and version-one schema.

## Approach

Use the existing immutable `NativeHueProfile` as the sole native selection authority.
Require it in the scene client, scene proxy, Entertainment relay and both launchers.
Reject missing or invalid selection before workers, sockets or state creation.
Govee-only installations continue to use their explicit validated target file.

Retaining source-only identity defaults would leave an accidental output path.
Filtering those defaults during archive construction would create two runtime
behaviors. Neither approach establishes one explicit selection contract.

## Compatibility and migration

Keep the four native roles, twelve channels, credential formats and profile schema.
Existing credential `light` and `area_id` hints do not override a validated profile.
The service configuration must specify `adapter.native_profile_file` with Hue
output. The source launcher must specify `--native-profile` with `--hue-config`.
Missing-profile configurations now fail with a safe diagnostic.

Reuse the validated same-selection profile created during the earlier extraction.
An older installation must receive a complete, independently verified profile
before activation. Do not infer enrollment from discovery, names or addresses.
Do not rewrite pairing, accessory identities, calibration, layout or power intent.
Deployment and physical acceptance remain outside this change.

## Verification

Test constructor and launcher rejection before IO. Check the actual archive for
literal physical UUIDs and execute its missing-profile startup and preflight paths.
Run scene requests, native membership checks and RGB16 encoding with synthetic
profiles. Retain existing power, foreign-owner, composition and shutdown contracts.
Preflight must leave configuration and saved state bytes unchanged.
