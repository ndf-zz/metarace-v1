# metarace-v1

Metarace cycle race tools for Python2/pygtk.

This project will be replaced by 
metarace (v2) for python3 and gtk/gi: https://github.com/ndf-zz/metarace/

Do not use.

## Pre-Requisites

System-provided packages:

	libatk-adaptor

## Testing

	$ cd src
	$ export PYTHONPATH=.
	$ export PYTHONDONTWRITEBYTECODE=yes
	$ python2 -m metarace.roadmeet

## Use with Mosquitto

Subscribe to all messages:

	$ mosquitto_sub -h localhost -t 'metarace/#' -v

Publish a passing:

	$ mosquitto_pub -h localhost -t metarace/timing -m ';host;MAN;riderno:2;tod'
