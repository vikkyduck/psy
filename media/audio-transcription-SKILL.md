---
name: audio-transcription
description: Transcribe received audio or video locally and derive meeting notes from the transcript when requested.
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [audio, transcription, meetings, russian]
---
# Audio transcription

Use the original recording supplied by the user. Large recordings can be selected on the sender's device through the Telegram Mini App. The owner can use the private menu or /audio. Group members can use the shared entry or /audio. Each group handoff is tied to its sender. The result goes to the destination shown when the sender starts the upload.

## Transcribe a local recording

Run the command below using the Python environment that contains Hermes, faster-whisper and its dependencies. Use an existing private directory for the output. Set the language to ru, en or auto as appropriate.

```sh
python "$HERMES_HOME/skills/media/audio-transcription/scripts/transcribe.py" "$INPUT" --output "$OUTPUT" --language ru
```

Check the worker's final JSON for complete=true and verify the duration and the nonempty UTF-8 output. The CPU decoder processes successive 60-second windows with overlap. Interrupting the process leaves a checkpoint. Repeating the same command resumes from that checkpoint. Keep the completion consumer active for long jobs. Use the saved_transcript path returned by the decoder when the caller requested a temporary export. A completed run can report that it detected no intelligible speech.

## Deliver the requested result

Read the full transcript before writing notes. Attach the transcript when requested. Ground decisions, agreements, deadline and owner in the spoken material. Include timestamps for important agreements. Keep uncertain names, speakers and unclear statements marked as uncertain. An empty or incomplete result leaves the content unverified. Only confirm a Telegram delivery after the worker has a successful send response. Send recordings through the local transcription path.

## Upload and access

The Mini App accepts up to 1 GiB per recording with a maximum duration of 24 hours. A sender can resume an interrupted upload, retain the original destination and track processing. An uncertain delivery is visible in the history. The sender can explicitly confirm a possible duplicate before requesting another send. The native attachment route and the Mini App have separate limits. If Telegram could not provide the source to the bot, ask the sender to select it through the Mini App.
