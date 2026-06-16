document.addEventListener("DOMContentLoaded", () => {
    // UI Elements
    const midiInputSelect = document.getElementById("midi-input");
    const midiOutputSelect = document.getElementById("midi-output");
    const splitPointInput = document.getElementById("split-point");
    const splitNoteName = document.getElementById("split-note-name");
    const compModeSelect = document.getElementById("comp-mode");
    const stylePresetSelect = document.getElementById("style-preset");
    const playbackModeSelect = document.getElementById("playback-mode");
    const tempoSlider = document.getElementById("tempo-slider");
    const tempoVal = document.getElementById("tempo-val");
    const retriggerInput = document.getElementById("retrigger-input");
    const saveSettingsBtn = document.getElementById("save-settings-btn");
    const findBpmBtn = document.getElementById("find-bpm-btn");
    let isDetectingBpm = false;
    let lastDetectedBpm = null;
    
    const progressionTriggerSelect = document.getElementById("progression-trigger");
    const triggerCcPanel = document.getElementById("trigger-cc-panel");
    const triggerNotePanel = document.getElementById("trigger-note-panel");
    const triggerPitchPanel = document.getElementById("trigger-pitch-panel");
    
    const ccControlInput = document.getElementById("cc-control");
    const ccArmValueInput = document.getElementById("cc-arm-value");
    const ccTriggerValueInput = document.getElementById("cc-trigger-value");
    const controlNoteInput = document.getElementById("control-note");
    const learnControlNoteCheckbox = document.getElementById("learn-control-note");
    const pitchThresholdInput = document.getElementById("pitch-threshold");
    const pitchResetInput = document.getElementById("pitch-reset");
    
    function updateTriggerPanels() {
        const val = progressionTriggerSelect.value;
        triggerCcPanel.style.display = (val === "cc-down") ? "flex" : "none";
        triggerNotePanel.style.display = (val === "control-note") ? "flex" : "none";
        triggerPitchPanel.style.display = (val === "pitch-up") ? "flex" : "none";
    }
    progressionTriggerSelect.addEventListener("change", updateTriggerPanels);
    
    const statusDot = document.getElementById("playback-status-dot");
    const statusText = document.getElementById("playback-status-text");
    const bpmVal = document.getElementById("bpm-val");
    const scaleVal = document.getElementById("scale-val");
    
    const hacUrlInput = document.getElementById("hac-url");
    const fetchBtn = document.getElementById("fetch-btn");
    
    const playBtn = document.getElementById("play-btn");
    const stopBtn = document.getElementById("stop-btn");
    const nextBtn = document.getElementById("next-btn");
    const resetBtn = document.getElementById("reset-btn");
    
    const activeChordName = document.getElementById("active-chord-name");
    const activeLyricText = document.getElementById("active-lyric-text");
    const nextLyricText = document.getElementById("next-lyric-text");
    
    const transposeDownBtn = document.getElementById("transpose-down-btn");
    const transposeUpBtn = document.getElementById("transpose-up-btn");
    const transposeValDisplay = document.getElementById("transpose-val-display");
    let currentTranspose = 0;
    
    const logBox = document.getElementById("log-box");
    const timelineContainer = document.getElementById("timeline-container");
    const pianoKeyboard = document.getElementById("piano-keyboard");
    
    // Note names for splitting display
    const NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];
    function getNoteName(noteNumber) {
        const octave = Math.floor(noteNumber / 12) - 1;
        const name = NOTE_NAMES[noteNumber % 12];
        return `${name}${octave}`;
    }

    // Initialize Virtual Piano Keyboard (61 Keys, C2 to C7, MIDI notes 36 to 96)
    const keyElements = {};
    function renderKeyboard() {
        pianoKeyboard.innerHTML = "";
        let whiteKeyIndex = 0;
        const totalWhiteKeys = 36;
        
        for (let note = 36; note <= 96; note++) {
            const pc = note % 12;
            const isBlack = [1, 3, 6, 8, 10].includes(pc);
            
            if (!isBlack) {
                const key = document.createElement("div");
                key.className = "piano-key-white";
                key.dataset.note = note;
                key.style.left = (whiteKeyIndex * (100 / totalWhiteKeys)) + "%";
                
                // Clicking triggers virtual note-on/off (simulation)
                key.addEventListener("mousedown", () => simulateMidiNote(note, 80));
                
                pianoKeyboard.appendChild(key);
                keyElements[note] = key;
                
                // Place black key relative to this white key
                if ([0, 1, 3, 4, 5].includes(whiteKeyIndex % 7) && note < 96) {
                    const blackNote = note + 1;
                    const blackKey = document.createElement("div");
                    blackKey.className = "piano-key-black";
                    blackKey.dataset.note = blackNote;
                    // Centered between white keys
                    blackKey.style.left = (((whiteKeyIndex + 1) * (100 / totalWhiteKeys)) - 1.1) + "%";
                    
                    blackKey.addEventListener("mousedown", () => simulateMidiNote(blackNote, 80));
                    
                    pianoKeyboard.appendChild(blackKey);
                    keyElements[blackNote] = blackKey;
                }
                
                whiteKeyIndex++;
            }
        }
    }
    
    renderKeyboard();
    
    function simulateMidiNote(note, velocity) {
        logMessage(`virtual`, `Virtual key clicked: ${getNoteName(note)} (${note})`, "sys");
        // Flash key visually
        const keyEl = keyElements[note];
        if (keyEl) {
            keyEl.classList.add("active-melody");
            setTimeout(() => keyEl.classList.remove("active-melody"), 250);
        }
    }

    // Helper to update split point label
    splitPointInput.addEventListener("input", (e) => {
        splitNoteName.textContent = getNoteName(parseInt(e.target.value));
    });
    tempoSlider.addEventListener("input", (e) => {
        tempoVal.textContent = e.target.value;
    });

    // Populate Ports
    async function loadPorts() {
        try {
            const res = await fetch("/api/ports");
            const data = await res.json();
            
            // Clear lists
            midiInputSelect.innerHTML = "";
            midiOutputSelect.innerHTML = '<option value="">Virtual Out (Default)</option>';
            
            if (data.inputs && data.inputs.length > 0) {
                data.inputs.forEach(port => {
                    const opt = document.createElement("option");
                    opt.value = port;
                    opt.textContent = port;
                    midiInputSelect.appendChild(opt);
                });
            } else {
                midiInputSelect.innerHTML = '<option value="">No inputs found</option>';
            }
            
            if (data.outputs && data.outputs.length > 0) {
                data.outputs.forEach(port => {
                    const opt = document.createElement("option");
                    opt.value = port;
                    opt.textContent = port;
                    midiOutputSelect.appendChild(opt);
                });
            }
        } catch (e) {
            logMessage("error", "Failed to fetch MIDI ports: " + e.message, "error");
        }
    }
    
    loadPorts().then(() => {
        connectSSE();
    });

    // Logger Utility
    function logMessage(source, message, type = "") {
        const row = document.createElement("div");
        row.className = `log-row ${type}`;
        const timeStr = new Date().toLocaleTimeString();
        row.textContent = `[${timeStr}] ${source.toUpperCase()}: ${message}`;
        logBox.appendChild(row);
        logBox.scrollTop = logBox.scrollHeight;
        
        // Limit logs to last 100 rows
        while (logBox.children.length > 100) {
            logBox.removeChild(logBox.firstChild);
        }
    }

    // Submit Settings to API
    async function saveSettings() {
        const payload = {
            input_port: midiInputSelect.value,
            output_port: midiOutputSelect.value,
            split: parseInt(splitPointInput.value),
            comp: compModeSelect.value,
            style: stylePresetSelect.value,
            mode: playbackModeSelect.value,
            tempo: parseFloat(tempoSlider.value),
            retrigger: parseFloat(retriggerInput.value),
            period: parseFloat(retriggerInput.value) * 3.0, // scale period or keep default
            progression_trigger: progressionTriggerSelect.value,
            cc_control: parseInt(ccControlInput.value),
            cc_arm_value: parseInt(ccArmValueInput.value),
            cc_trigger_value: parseInt(ccTriggerValueInput.value),
            control_note: parseInt(controlNoteInput.value),
            learn_control_note: learnControlNoteCheckbox.checked,
            pitch_threshold: parseInt(pitchThresholdInput.value),
            pitch_reset: parseInt(pitchResetInput.value),
            transpose: currentTranspose
        };
        
        try {
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.status === "ok") {
                logMessage("sys", "Settings successfully updated.", "sys");
            } else {
                logMessage("sys", "Error updating settings.", "error");
            }
        } catch (e) {
            logMessage("sys", "Error sending settings: " + e.message, "error");
        }
    }
    
    saveSettingsBtn.addEventListener("click", saveSettings);
    
    findBpmBtn.addEventListener("click", async () => {
        if (!isDetectingBpm) {
            try {
                const res = await fetch("/api/bpm_detection", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "start" })
                });
                const data = await res.json();
                if (data.status === "ok") {
                    isDetectingBpm = true;
                    findBpmBtn.classList.remove("btn-secondary");
                    findBpmBtn.classList.add("btn-accent");
                    findBpmBtn.classList.add("detecting");
                    findBpmBtn.textContent = "Listening... Play beats";
                    logMessage("sys", "BPM detection started. Play steady notes on piano.", "sys");
                }
            } catch (err) {
                console.error("Error starting BPM detection", err);
            }
        } else {
            try {
                const res = await fetch("/api/bpm_detection", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "apply", bpm: lastDetectedBpm })
                });
                const data = await res.json();
                if (data.status === "ok") {
                    isDetectingBpm = false;
                    findBpmBtn.classList.remove("btn-accent");
                    findBpmBtn.classList.remove("detecting");
                    findBpmBtn.classList.add("btn-secondary");
                    findBpmBtn.textContent = "Find BPM from Play";
                    logMessage("sys", `BPM detection applied: ${lastDetectedBpm ? Math.round(lastDetectedBpm) + ' BPM' : 'none'}`, "sys");
                }
            } catch (err) {
                console.error("Error applying BPM", err);
            }
        }
    });
    
    // Auto-save settings on input/selection change
    [
        midiInputSelect, midiOutputSelect, splitPointInput, compModeSelect, stylePresetSelect,
        playbackModeSelect, tempoSlider, retriggerInput, progressionTriggerSelect,
        ccControlInput, ccArmValueInput, ccTriggerValueInput, controlNoteInput,
        learnControlNoteCheckbox, pitchThresholdInput, pitchResetInput
    ].forEach(input => {
        if (input) {
            input.addEventListener("change", saveSettings);
        }
    });

    async function updateTranspose(delta) {
        currentTranspose = Math.max(-12, Math.min(12, currentTranspose + delta));
        transposeValDisplay.textContent = (currentTranspose > 0 ? "+" : "") + currentTranspose;
        
        await saveSettings();
    }
    
    transposeDownBtn.addEventListener("click", () => updateTranspose(-1));
    transposeUpBtn.addEventListener("click", () => updateTranspose(1));

    // Fetch HopAmChuan Song
    async function fetchSong() {
        const url = hacUrlInput.value.trim();
        if (!url) {
            alert("Please paste a valid hopamchuan url.");
            return;
        }
        
        fetchBtn.disabled = true;
        fetchBtn.textContent = "Fetching...";
        logMessage("sys", `Requesting HopAmChuan URL: ${url}`, "sys");
        
        try {
            const res = await fetch("/api/fetch-hopamchuan", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url: url })
            });
            const data = await res.json();
            if (data.status === "ok") {
                logMessage("sys", `Successfully fetched song timeline.`, "sys");
            } else {
                logMessage("sys", `Failed to parse song: ${data.message}`, "error");
                alert("Error fetching song: " + data.message);
            }
        } catch (e) {
            logMessage("sys", "Error connecting to backend: " + e.message, "error");
        } finally {
            fetchBtn.disabled = false;
            fetchBtn.textContent = "Fetch Chords & Lyrics";
        }
    }
    
    fetchBtn.addEventListener("click", fetchSong);

    // Playback Controls
    async function sendControl(command) {
        try {
            const res = await fetch("/api/control", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ command: command })
            });
            const data = await res.json();
            logMessage("sys", `Control command [${command.toUpperCase()}] sent.`, "sys");
        } catch (e) {
            logMessage("sys", `Error sending control: ${e.message}`, "error");
        }
    }
    
    playBtn.addEventListener("click", () => sendControl("play"));
    stopBtn.addEventListener("click", () => sendControl("stop"));
    nextBtn.addEventListener("click", () => sendControl("next"));
    resetBtn.addEventListener("click", () => sendControl("reset"));

    // Render Timeline View
    let timelineEvents = [];
    function renderTimeline(events) {
        timelineContainer.innerHTML = "";
        timelineEvents = events;
        
        if (!events || events.length === 0) {
            timelineContainer.innerHTML = '<div class="timeline-empty">No chords/lyrics timeline loaded.</div>';
            return;
        }
        
        events.forEach((evt, idx) => {
            const row = document.createElement("div");
            row.className = "timeline-row";
            row.id = `timeline-row-${idx}`;
            
            // Render minutes:seconds
            const mins = Math.floor(evt.time_s / 60);
            const secs = Math.floor(evt.time_s % 60);
            const timeFormatted = `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
            
            const timeCol = document.createElement("div");
            timeCol.className = "timeline-time";
            timeCol.textContent = timeFormatted;
            
            const chordCol = document.createElement("div");
            chordCol.className = "timeline-chord";
            chordCol.textContent = evt.chord;
            
            const sectionCol = document.createElement("div");
            sectionCol.className = "timeline-section" + (evt.section === "chorus" ? " chorus-tag" : "");
            sectionCol.textContent = evt.section || "verse";
            
            const lyricCol = document.createElement("div");
            lyricCol.className = "timeline-lyric";
            lyricCol.textContent = evt.lyric;
            
            row.appendChild(timeCol);
            row.appendChild(chordCol);
            row.appendChild(sectionCol);
            row.appendChild(lyricCol);
            
            // Timeline row click jumps to chord index
            row.addEventListener("click", () => jumpToTimelineIndex(idx));
            
            timelineContainer.appendChild(row);
        });
    }
    
    async function jumpToTimelineIndex(index) {
        try {
            await fetch("/api/control", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ command: "jump", index: index })
            });
            logMessage("sys", `Jumped to chord index ${index + 1}`, "sys");
        } catch (e) {
            logMessage("sys", `Error jumping: ${e.message}`, "error");
        }
    }

    // Connect to Server-Sent Events (SSE) Stream
    function connectSSE() {
        logMessage("sys", "Connecting to SSE stream...", "sys");
        const sse = new EventSource("/api/events");
        
        sse.onopen = () => {
            logMessage("sys", "Connected to Event Stream. Monitoring MIDI...", "sys");
            statusDot.classList.add("active");
            statusText.textContent = "Engine Connected";
        };
        
        sse.onerror = (e) => {
            logMessage("sys", "SSE Connection lost. Retrying...", "error");
            statusDot.classList.remove("active");
            statusText.textContent = "Disconnected";
            // SSE automatically retries, but we clear indicators
            activeChordName.textContent = "--";
            activeLyricText.textContent = "Disconnected from server.";
        };
        
        sse.onmessage = (event) => {
            // Check if ping keepalive
            if (event.data.startsWith(":")) return;
            
            const payload = JSON.parse(event.data);
            const eventType = payload.event;
            const data = payload.data;
            
            switch (eventType) {
                case "state":
                    // Initial load configuration
                    if (data.input_port) midiInputSelect.value = data.input_port;
                    if (data.output_port) midiOutputSelect.value = data.output_port;
                    splitPointInput.value = data.split || 60;
                    splitNoteName.textContent = getNoteName(data.split || 60);
                    compModeSelect.value = data.comp || "style";
                    stylePresetSelect.value = data.style || "yiruma";
                    playbackModeSelect.value = data.mode || "lyric";
                    
                    if (data.progression_trigger) progressionTriggerSelect.value = data.progression_trigger;
                    if (data.cc_control !== undefined) ccControlInput.value = data.cc_control;
                    if (data.cc_arm_value !== undefined) ccArmValueInput.value = data.cc_arm_value;
                    if (data.cc_trigger_value !== undefined) ccTriggerValueInput.value = data.cc_trigger_value;
                    if (data.control_note !== undefined) controlNoteInput.value = data.control_note;
                    if (data.learn_control_note !== undefined) learnControlNoteCheckbox.checked = data.learn_control_note;
                    if (data.pitch_threshold !== undefined) pitchThresholdInput.value = data.pitch_threshold;
                    if (data.pitch_reset !== undefined) pitchResetInput.value = data.pitch_reset;
                    if (data.transpose !== undefined) {
                        currentTranspose = data.transpose;
                        transposeValDisplay.textContent = (currentTranspose > 0 ? "+" : "") + currentTranspose;
                    }
                    if (data.detecting_bpm) {
                        isDetectingBpm = true;
                        findBpmBtn.classList.remove("btn-secondary");
                        findBpmBtn.classList.add("btn-accent");
                        findBpmBtn.classList.add("detecting");
                        findBpmBtn.textContent = "Listening... Play beats";
                    }
                    if (data.current_bpm) {
                        lastDetectedBpm = data.current_bpm;
                        bpmVal.textContent = Math.round(data.current_bpm);
                        scaleVal.textContent = parseFloat(data.time_scale || 1.0).toFixed(2);
                        tempoSlider.value = parseFloat(data.time_scale || 1.0).toFixed(2);
                        tempoVal.textContent = parseFloat(data.time_scale || 1.0).toFixed(2);
                    }
                    updateTriggerPanels();
                    
                    if (data.is_playing) {
                        statusDot.classList.add("active");
                        statusText.textContent = "Engine Running";
                    }
                    
                    if (data.fetched_url) {
                        hacUrlInput.value = data.fetched_url;
                        renderTimeline(data.fetched_events);
                        highlightTimelineIndex(data.progression_index);
                    }
                    break;
                    
                case "bpm_status":
                    if (data.detecting) {
                        isDetectingBpm = true;
                        findBpmBtn.classList.remove("btn-secondary");
                        findBpmBtn.classList.add("btn-accent");
                        findBpmBtn.classList.add("detecting");
                        findBpmBtn.textContent = "Listening... Play beats";
                    } else {
                        isDetectingBpm = false;
                        findBpmBtn.classList.remove("btn-accent");
                        findBpmBtn.classList.remove("detecting");
                        findBpmBtn.classList.add("btn-secondary");
                        findBpmBtn.textContent = "Find BPM from Play";
                    }
                    break;
                    
                case "bpm_update":
                    lastDetectedBpm = data.bpm;
                    bpmVal.textContent = Math.round(data.bpm);
                    scaleVal.textContent = parseFloat(data.time_scale).toFixed(2);
                    tempoSlider.value = parseFloat(data.time_scale).toFixed(2);
                    tempoVal.textContent = parseFloat(data.time_scale).toFixed(2);
                    findBpmBtn.textContent = `Listening... ${Math.round(data.bpm)} BPM (Click to Set)`;
                    break;

                case "status":
                    if (data.is_playing) {
                        statusDot.classList.add("active");
                        statusText.textContent = "Engine Running";
                        logMessage("sys", "Accompanist playback active.", "sys");
                    } else {
                        statusDot.classList.remove("active");
                        statusText.textContent = "Engine Stopped";
                        logMessage("sys", "Accompanist playback stopped.", "sys");
                        // Clear keyboard on stop
                        document.querySelectorAll(".piano-key-white, .piano-key-black").forEach(el => {
                            el.classList.remove("active-melody", "active-chord");
                        });
                    }
                    break;
                    
                case "midi_event":
                    // Visual Keypress Animations on Piano
                    const keyEl = keyElements[data.note];
                    if (keyEl) {
                        const styleClass = (data.source === "output" || (data.source === "input" && data.hand === "left")) 
                            ? "active-chord" 
                            : "active-melody";
                            
                        if (data.event === "note_on" && data.velocity > 0) {
                            keyEl.classList.add(styleClass);
                        } else {
                            keyEl.classList.remove("active-melody", "active-chord");
                        }
                    }
                    
                    // Optional logging for note events
                    if (data.event === "note_on" && data.velocity > 0) {
                        const noteDesc = `${getNoteName(data.note)} (vel=${data.velocity})`;
                        logMessage(data.source, `${data.event.toUpperCase()} - ${noteDesc} [${data.hand || "any"}]`, data.source === "input" ? "midi-in" : "midi-out");
                    }
                    break;
                    
                case "decision":
                    // Auto-chord triggered
                    activeChordName.textContent = data.chosen_chord;
                    
                    // If lyric exists, highlight chord in lyrics
                    if (data.lyric) {
                        // Reconstruct lyrics with inline chord tags
                        const chordRegex = new RegExp(`\\[${data.chosen_chord}\\]`, "g");
                        let renderedLyric = data.lyric;
                        // Format active lyric nicely
                        activeLyricText.innerHTML = `<span class="lyric-chord-tag">${data.chosen_chord}</span> ${data.lyric}`;
                    } else {
                        activeLyricText.textContent = `Auto chord triggered.`;
                    }
                    
                    // Display next chord/lyric prediction
                    if (data.progression_index !== undefined && timelineEvents && timelineEvents.length > 0) {
                        const nextIdx = data.progression_index;
                        if (nextIdx < timelineEvents.length) {
                            const nextEvt = timelineEvents[nextIdx];
                            const nextChordPart = nextEvt.chord ? `<span class="lyric-chord-tag" style="background-color: rgba(255, 255, 255, 0.05); color: var(--text-secondary); border-color: rgba(255, 255, 255, 0.1);">${nextEvt.chord}</span> ` : '';
                            nextLyricText.innerHTML = `${nextChordPart}${nextEvt.lyric || '<em>[Instrumental]</em>'}`;
                        } else {
                            nextLyricText.textContent = "End of song.";
                        }
                    } else {
                        nextLyricText.textContent = "--";
                    }
                    
                    // Highlight row in timeline
                    if (data.progression_index !== undefined) {
                        highlightTimelineIndex(data.progression_index - 1);
                    }
                    
                    // Update stats
                    if (data.bpm) bpmVal.textContent = Math.round(data.bpm);
                    if (data.time_scale) scaleVal.textContent = parseFloat(data.time_scale).toFixed(2);
                    
                    logMessage("engine", `Auto chord ${data.chosen_chord} for melody [Candidates: ${data.candidate_count}]`, "midi-out");
                    break;
                    
                case "timeline":
                    renderTimeline(data.events);
                    hacUrlInput.value = data.url;
                    break;
                    
                case "reset":
                    highlightTimelineIndex(data.index);
                    if (data.index === 0) {
                        activeChordName.textContent = "--";
                        activeLyricText.textContent = "Timeline reset to beginning. Press play to start.";
                        nextLyricText.textContent = "--";
                    }
                    break;
                    
                case "error":
                    logMessage("error", data.message, "error");
                    break;
            }
        };
    }
    
    
    
    function highlightTimelineIndex(index) {
        // Remove active class from all rows
        document.querySelectorAll(".timeline-row").forEach(row => {
            row.classList.remove("active");
        });
        
        const activeRow = document.getElementById(`timeline-row-${index}`);
        if (activeRow) {
            activeRow.classList.add("active");
            // Scroll to keep active row centered
            activeRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
    }
});
