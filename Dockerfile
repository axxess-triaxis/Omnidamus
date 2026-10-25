FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml ${LAMBDA_TASK_ROOT}/pyproject.toml
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

RUN pip install --no-cache-dir "${LAMBDA_TASK_ROOT}" -t ${LAMBDA_TASK_ROOT}

CMD ["lambda_handler.handler"]
