Build a complete, production-ready Docker application called "audio-normalizer", a self-hosted web app that normalize audio files (mp3, flax, avi, etc.). 
if possible use Intel VAAPI hardware acceleration from Synology DS918+ NAS (Intel Celeron J3455 Apollo Lake, Intel HD Graphics 500)

Target deployment: Synology DS918+ NAS (Intel Celeron J3455 Apollo Lake, Intel HD Graphics 500) via Docker/docker-compose.

functionality:
- application will check all audio files in give directory (including sub directories) and normalize the audio output level, so all audio files will have the same sound level
- if possible make use of hardware transcoding from synology nas ds918+
- audio quality should not be changed (only audio sound level)
- add schedule so application will only run on certain days, weeks, time
- if new audio files are added, application should detect then and normalize audio level automatically (also after restart of the app, so information should be persistent)
- application should give warnings and ask for user confirmation if audio levels needs to be changed really a lot for a file,
  but application should not wait for user conformation, but park that file and continue with the next audio file.
- provide native home assistant interface (API) to give main (most important) information/parameters to home assistant sensors:
    1- status (running, pause, stopped, % complete)
    2- total files to be checked
    3- files already check
    4- files changed
    any other important parameter


web page:

base location of audio files can be set by user (provide mount in docker compose file)
user enable/disable directories (including select all / select none), also show windows like directory treestructure.
provide start / stop / browse function
user should set default output sound level (slider and value)
option to override the user confirmation for large changes
setting page for home assistant (including access token and sensor definitions)
give dashboard with:
  1- how many directories/files will be checked
  2- how many file needs to be done
  3- how many files are checked
  4- how many files are actually checked
user can configure schedule that application will run


advise on the best structure and confirm with user before implementation. in case questions or multiple solutions ask user.
first build mock-up of web site before full implementation
