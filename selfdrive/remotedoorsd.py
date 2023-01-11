#!/usr/bin/env python3
import time
import threading
from flask import Flask
from common.params import Params

app = Flask(__name__)

index = """
<html>
  <head>
    <meta name="viewport" content="initial-scale=1, width=device-width"/>
    <title>remote door locker</title>
    <style>
    .button {
      border: none;
      color: white;
      padding: 16px 32px;
      text-align: center;
      text-decoration: none;
      display: inline-block;
      font-size: 16px;
      margin: 4px 2px;
      transition-duration: 0.4s;
      cursor: pointer;
    }

    .button1 {
      background-color: white; 
      color: black; 
      border: 2px solid #4CAF50;
    }

    .button1:hover {
      background-color: #4CAF50;
      color: white;
    }

    .button2 {
      background-color: white; 
      color: black; 
      border: 2px solid #008CBA;
    }

    .button2:hover {
      background-color: #008CBA;
      color: white;
    }

    .container { 
      height: 200px;
      position: relative;
      border: 3px solid green; 
    }

    .center {
      margin: 0;
      position: absolute;
      top: 50%;
      left: 50%;
      -ms-transform: translate(-50%, -50%);
      transform: translate(-50%, -50%);
    }

    </style>

  </head>
  <body>
    <script type="text/javascript">
      function lockCommand() {
        document.getElementById("demo").innerHTML = "Hello World";
        let xhr = new XMLHttpRequest();
        xhr.open("GET", "/lock");
        xhr.send();
      }
      function unlockCommand() {
        document.getElementById("demo").innerHTML = "Bye World";
        let xhr = new XMLHttpRequest();
        xhr.open("GET", "/unlock");
        xhr.send();
      }
    </script>
    <h1>Remote Door Un/Locker</h1>

    <p>Foo.</p>
    <p><strong>Bar:</strong>TODO</p>
    <p id="demo"></p>

    <div class="container">
      <div class="center">
        <button onclick="lockCommand()" class="button button1">Lock</button> <br><br>
        <button onclick="unlockCommand()" class="button button2">Unlock</button>
      </div>
    </div>
  </body>
</html>
"""

@app.route("/")
def hello_world():
  return index

last_send_time = time.monotonic()
@app.route("/lock")
def lock():
  Params().put_bool("AleSato_HelloButton", True)
  return "locked"

@app.route("/unlock")
def unlock():
  Params().put_bool("AleSato_HelloButton", False)
  return "unlocked"

def handle_timeout():
  while 1:
    this_time = time.monotonic()
    if (last_send_time+0.5) < this_time:
      print("timeout, no web in %.2f s" % (this_time-last_send_time))
    time.sleep(0.1)

def main():
  threading.Thread(target=handle_timeout, daemon=True).start()
  app.run(host="0.0.0.0")

if __name__ == '__main__':
  main()
