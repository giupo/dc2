"""DynaCamp: a headless dynamic military campaign engine.

The campaign (fronts, logistics, air war, air defence) runs on its own in this
package; games plug in through :mod:`dynacamp.adapters` and take over whatever
is inside a player's bubble. The HTTP API in :mod:`dynacamp.api` serves the web
front-end.

Main entry points:

* :func:`dynacamp.scenario.load` - build a :class:`~dynacamp.state.CampaignState`
  from a TOML scenario;
* :class:`dynacamp.engine.Engine` - advance the campaign and mediate handoffs
  with a game adapter;
* :func:`dynacamp.cli.main` - the ``dynacamp`` command line.
"""

