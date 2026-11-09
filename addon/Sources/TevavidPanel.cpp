#include "TevavidPanel.hpp"

#include "APIEnvir.h"
#include "ACAPinc.h"
#include "DGModule.hpp"
#include "File.hpp"
#include "FileSystem.hpp"
#include "Guid.hpp"
#include "ObjectState.hpp"
#include "ObjectStateJSONConversion.hpp"
#include "Process.hpp"
#include "ScriptUIPalette.hpp"

#include <memory>
#include <vector>

namespace Tevavid {

static const Int32 DefaultPort = 8765;

// The started service processes are kept, so their handles live as long as the Add-On.
static std::vector<std::unique_ptr<GS::Process>>& StartedProcesses ()
{
    static std::vector<std::unique_ptr<GS::Process>> processes;
    return processes;
}

static IO::Location DataFolder ()
{
    IO::Location home;
    IO::fileSystem.GetSpecialLocation (IO::FileSystem::UserHome, &home);
    home.AppendToLocal (IO::Name (".tevavid-tools"));
    return home;
}

static bool Exists (const IO::Location& location)
{
    bool exists = false;
    return IO::fileSystem.Contains (location, &exists) == NoError && exists;
}

static bool ReadJson (const IO::Location& location, GS::ObjectState& os)
{
    if (!Exists (location)) {
        return false;
    }
    IO::File file (location);
    if (file.Open (IO::File::OpenMode::ReadMode) != NoError) {
        return false;
    }
    const GSErrCode err = JSON::ConvertToObjectState (file, os);
    file.Close ();
    return err == NoError && !os.IsEmpty ();
}

// The installed app's executable, from install.json written by the Tevavid Tools setup.
static bool AppExecutable (GS::UniString& path)
{
    IO::Location manifestLoc = DataFolder ();
    manifestLoc.AppendToLocal (IO::Name ("install.json"));
    GS::ObjectState manifest;
    GS::UniString installDir;
    if (!ReadJson (manifestLoc, manifest) || !manifest.Get ("installDir", installDir) || installDir.IsEmpty ()) {
        return false;
    }
    IO::Location executable (installDir);
#if defined (macintosh)
    executable.AppendToLocal (IO::Name ("Contents"));
    executable.AppendToLocal (IO::Name ("MacOS"));
    executable.AppendToLocal (IO::Name ("TevavidTools"));
#else
    executable.AppendToLocal (IO::Name ("TevavidTools.exe"));
#endif
    if (!Exists (executable)) {
        return false;
    }
    executable.ToPath (&path);
    return true;
}

// The port and token of the running (or last) service, or a new token for a service about to start.
static void SessionOf (Int32& port, GS::UniString& token)
{
    port = DefaultPort;
    token.Clear ();
    IO::Location sessionLoc = DataFolder ();
    sessionLoc.AppendToLocal (IO::Name ("session.json"));
    GS::ObjectState session;
    if (ReadJson (sessionLoc, session)) {
        Int32 savedPort = 0;
        if (session.Get ("port", savedPort) && savedPort > 0 && savedPort < 65536) {
            port = savedPort;
        }
        session.Get ("token", token);
    }
    if (token.GetLength () < 16) {
        GS::Guid guid;
        guid.Generate ();
        token = guid.ToUniString ();
        token.ReplaceAll ("{", "");
        token.ReplaceAll ("}", "");
        token.ReplaceAll ("-", "");
    }
}

// Starts "TevavidTools panel --background --no-open" with the given port and token. When the service already
// runs, the new process sees it and ends at once; otherwise it becomes the service with this token.
static bool Launch (Int32 port, const GS::UniString& token)
{
    GS::UniString executable;
    if (!AppExecutable (executable)) {
        return false;
    }
    GS::Array<GS::UniString> arguments;
    arguments.Push ("panel");
    arguments.Push ("--background");
    arguments.Push ("--no-open");
    arguments.Push ("--exit-with-archicad");
    arguments.Push ("--ui-port");
    arguments.Push (GS::ValueToUniString (port));
    arguments.Push ("--panel-token");
    arguments.Push (token);
    try {
        auto process = std::make_unique<GS::Process> (GS::Process::Create (executable, arguments, GS::Process::CreateNoWindow));
        if (!process->IsValid ()) {
            return false;
        }
        StartedProcesses ().push_back (std::move (process));
        return true;
    } catch (...) {
        return false;
    }
}

bool StartService ()
{
    Int32 port = DefaultPort;
    GS::UniString token;
    SessionOf (port, token);
    return Launch (port, token);
}

// A page that waits for the service to answer, then opens the panel. The palette shows it at once,
// so the menu never waits for the app to start.
static GS::UniString LoaderPage (Int32 port, const GS::UniString& token, const char* tab, bool appFound)
{
    const GS::UniString base = GS::UniString ("http://127.0.0.1:") + GS::ValueToUniString (port);
    const GS::UniString url = base + "/?t=" + token + "&mode=full&palette=1&tab=" + GS::UniString (tab);
    const GS::UniString missing = appFound ? "" :
        "Tevavid Tools is not installed for this user, or it was moved. Run the Tevavid Tools setup again.";
    return GS::UniString () +
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Tevavid</title>"
        "<style>body{margin:0;padding:28px 22px;font:14px/1.5 -apple-system,'Segoe UI',sans-serif;color:#555;"
        "background:#fff}@media(prefers-color-scheme:dark){body{background:#17191c;color:#aaa}}"
        "b{display:block;color:#111;font-size:16px;margin-bottom:6px}@media(prefers-color-scheme:dark){b{color:#eee}}</style>"
        "</head><body><b>Tevavid Tools</b><div id=\"s\">Starting...</div><script>"
        "var base='" + base + "',url='" + url + "',missing='" + missing + "',tries=0;"
        "function fail(t){document.getElementById('s').textContent=t;}"
        "function go(){tries++;fetch(base+'/api/ping',{mode:'no-cors',cache:'no-store'})"
        ".then(function(){location.replace(url);})"
        ".catch(function(){if(missing&&tries>3){fail(missing);return;}"
        "if(tries<150){setTimeout(go,400);}else{fail('Tevavid Tools did not start. Open Tevavid Tools from the Start menu "
        "or the Applications folder, or run its setup again.');}});}"
        "go();</script></body></html>";
}

void OpenPanel (const char* tab)
{
    Int32 port = DefaultPort;
    GS::UniString token;
    SessionOf (port, token);
    const bool appFound = Launch (port, token);

    ScriptUIOptions options;
    options.width = 420;
    options.height = 760;
    options.title = "Tevavid";
    options.scrollBarsVisible = false;
    options.contextMenuEnabled = false;
    ScriptUIPalette::Instance (0).ShowWithHTML (LoaderPage (port, token, tab, appFound), options);
}

}
