# ros-pointperfect

Quick and dirty evaluation code for openmower. Just disable Ntrip client (OM_USE_NTRIP=False) and this component will publish Point Perfect messages to /ntrip_client/rtcm instead.

This is not integrated with openmower stack because it's just a POC for evaluation purpose. You'll have to run it in parallel of make the integration effort if useful.

Prerequisite: Point Perfect subscription and configuration json downloaded from Ublox account.