#pragma once

#include "UniString.hpp"

// The Tevavid menu's link to the Tevavid Tools app (the panel and its local web service).
//
// The app keeps ~/.tevavid-tools/install.json (where it is installed) and, while its service runs,
// ~/.tevavid-tools/session.json (the port and the token the panel's pages need). The menu starts
// the service when needed and shows the panel page in a Script UI palette (Archicad's own browser).
namespace Tevavid {

// Starts the Tevavid Tools service in the background, without opening the panel. A second start
// while it already runs ends at once, so this is cheap to call. Returns false when the app is not
// installed.
bool StartService ();

// Shows the panel on one of its tabs: "chat", "sun", "site", "import", "tools", "library", "settings".
void OpenPanel (const char* tab);

}
