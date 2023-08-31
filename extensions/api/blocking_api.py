import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from extensions.api.util import build_parameters, try_start_cloudflared
from modules import shared
from modules.chat import generate_chat_reply, delete_character
from modules.LoRA import add_lora_to_model
from modules.models import load_model, unload_model
from modules.models_settings import (
    get_model_settings_from_yamls,
    update_model_parameters
)
from modules.text_generation import (
    encode,
    generate_reply,
    stop_everything_event
)
from modules.utils import get_available_models, get_available_characters
from modules.chat import save_character
from modules.logging_colors import logger


def get_model_info():
    return {
        'model_name': shared.model_name,
        'lora_names': shared.lora_names,
        # dump
        'shared.settings': shared.settings,
        'shared.args': vars(shared.args),
    }

class Handler(BaseHTTPRequestHandler):

    def simple_json_results(self, resp):
        logger.debug("Preparing simple JSON results response")
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()

        response = json.dumps({
            'results': resp,
        })

        self.wfile.write(response.encode('utf-8'))

    def auth(self):
        if 'Authorization' not in self.headers:
            logger.warning("Authorization header missing")
            self.send_error(401)
            return

        auth_header = self.headers.get('Authorization')
        logger.debug(f"Received Authorization header: {auth_header}")
        token = auth_header.replace('Bearer ', '')

        if token != shared.args.auth_api_token:
            logger.warning("Invalid auth token provided")
            self.send_error(401)
            return

    def do_GET(self):
        logger.info(f"Received GET request for path: {self.path}")
        
        if shared.args.auth_api:
            self.auth()

        if self.path == '/api/v1/model':
            self.send_response(200)
            self.end_headers()
            response = json.dumps({
                'result': shared.model_name
            })
            logger.debug(f"Response prepared: {response}")
            self.wfile.write(response.encode('utf-8'))
            
        elif self.path.startswith('/api/v1/characters'):
            logger.debug("Fetching available characters")
            self.simple_json_results(get_available_characters()[1:])
        else:
            logger.warning(f"Invalid path: {self.path}")
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        try:
            body = json.loads(self.rfile.read(content_length).decode('utf-8'))
            logger.debug(f"Received POST body: {body}")
        except Exception as e:
            logger.error(f"Error parsing POST body: {e}")
            self.send_error(400)
            return

        if shared.args.auth_api:
            self.auth()

        if self.path == '/api/v1/generate':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            prompt = body['prompt']
            generate_params = build_parameters(body)
            stopping_strings = generate_params.pop('stopping_strings')
            generate_params['stream'] = False

            generator = generate_reply(
                prompt, generate_params, stopping_strings=stopping_strings, is_chat=False)

            answer = ''
            for a in generator:
                answer = a

            response = json.dumps({
                'results': [{
                    'text': answer
                }]
            })

            logger.debug(f"Response prepared: {response}")
            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/character':
            logger.debug("Processing POST for single character addition")
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            name = body['name']
            context = body['context']
            greeting = body['greeting']
            example_dialogue = body['example_dialogue']
            context += f"\n{example_dialogue.strip()}\n"

            save_character(name, greeting, context, None, name)

            response = json.dumps({
                'results': [{
                    'text': f'character "{name}" saved with success'
                }]
            })

            logger.debug(f"Saved character: {name}")
            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/characters':
            logger.debug("Processing POST for batch character addition")
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            if body is None:
                logger.warning("Body is missing for batch character addition")
                self.simple_json_results("body is None")
                return

            characters = get_available_characters()
            for character in characters:
                delete_character(character)
                logger.debug(f"Deleted character: {character}")

            for character in body:
                try:
                    name = character['name']
                    context = character['context']
                    greeting = character['greeting']
                    example_dialogue = character['example_dialogue']
                    context += f"\n{example_dialogue.strip()}\n"

                    save_character(name, greeting, context, None, name)
                    logger.debug(f"Saved character: {name}")
                except Exception as e:
                    logger.error(f"Error saving character: {e}")
            
            response = json.dumps({
                'results': get_available_characters()[1:],
            })
            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/chat':
            logger.debug("Processing POST for chat interaction")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            user_input = body['user_input']
            regenerate = body.get('regenerate', False)
            _continue = body.get('_continue', False)

            generate_params = build_parameters(body, chat=True)
            generate_params['stream'] = False

            generator = generate_chat_reply(
                user_input, generate_params, regenerate=regenerate, _continue=_continue, loading_message=False)

            answer = generate_params['history']
            for a in generator:
                answer = a

            response = json.dumps({
                'results': [{
                    'history': answer
                }]
            })

            logger.debug(f"Chat reply generated for input: {user_input}")
            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/stop-stream':
            logger.debug("Processing POST to stop stream")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            stop_everything_event()

            response = json.dumps({
                'results': 'success'
            })

            logger.debug("Stream stopped successfully")
            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/model':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            # by default return the same as the GET interface
            result = shared.model_name

            # Actions: info, load, list, unload
            action = body.get('action', '')

            if action == 'load':
                model_name = body['model_name']
                args = body.get('args', {})
                print('args', args)
                for k in args:
                    setattr(shared.args, k, args[k])

                shared.model_name = model_name
                unload_model()

                model_settings = get_model_settings_from_yamls(shared.model_name)
                shared.settings.update(model_settings)
                update_model_parameters(model_settings, initial=True)

                if shared.settings['mode'] != 'instruct':
                    shared.settings['instruction_template'] = None

                try:
                    shared.model, shared.tokenizer = load_model(shared.model_name)
                    if shared.args.lora:
                        add_lora_to_model(shared.args.lora)  # list

                except Exception as e:
                    response = json.dumps({'error': {'message': repr(e)}})

                    self.wfile.write(response.encode('utf-8'))
                    raise e

                shared.args.model = shared.model_name

                result = get_model_info()

            elif action == 'unload':
                unload_model()
                shared.model_name = None
                shared.args.model = None
                result = get_model_info()

            elif action == 'list':
                result = get_available_models()

            elif action == 'info':
                result = get_model_info()

            response = json.dumps({
                'result': result,
            })

            self.wfile.write(response.encode('utf-8'))

        elif self.path == '/api/v1/token-count':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            tokens = encode(body['prompt'])[0]
            response = json.dumps({
                'results': [{
                    'tokens': len(tokens)
                }]
            })

            self.wfile.write(response.encode('utf-8'))
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()


def _run_server(port: int, share: bool = False):
    address = '0.0.0.0' if shared.args.listen else '127.0.0.1'

    server = ThreadingHTTPServer((address, port), Handler)

    def on_start(public_url: str):
        print(f'Starting non-streaming server at public url {public_url}/api')

    if share:
        try:
            try_start_cloudflared(port, max_attempts=3, on_start=on_start)
        except Exception:
            pass
    else:
        print(f'Starting API at http://{address}:{port}/api')        
    
    if shared.args.auth_api:
        print(f'with token {shared.args.auth_api_token}')

    server.serve_forever()


def start_server(port: int, share: bool = False):
    Thread(target=_run_server, args=[port, share], daemon=True).start()
