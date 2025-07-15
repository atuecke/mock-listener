# Mock Listener
A Mock Listener scripts for stress testing the [Aggregator Container](https://github.com/atuecke/aggregator-pi). These scripts are used to immitate multiple listeners, simultaneously streaming (or uploading) audio to an endpoint on the container. Make sure you have a mock audio .wav file saved to send. I would recommend downloading [this](https://xeno-canto.org/169082) and trimming it to 10-20 seconds.

## Running the test:
Run `stress_test_stream.py` with the following arguments:

`--file`: The path to the audio file you want to repeatedly send

`--num-sources`: The number of listeners you want to simulate sending **simultaneously**

`--interval`: The number seconds to wait between each send. For a full duty cycle, this would be zero

`--stagger`: The timespan to randomly stagger the emulated listeners. This stops all of them from sending at the exact same time, which is more realistic

`--url`: The endpoint to send to. In this case, it would be `http://localhost:8000/recordings_stream` because you are running the container locally

For example: `python stress_test_stream.py --file ./wav_samples/sample.wav --num-sources 5 --interval 2 --stagger 10 --url http://localhost:8000/recordings_stream` would randomly stagger 5 asynchronous emulated listeners across 10 seconds, with 2 seconds break between re-sending the file for each one.


You can also run the old `stress_test.py` that sends the entire file at once instead of streaming, whicht the container still supports, but for full testing I would recommend following the steps above.
