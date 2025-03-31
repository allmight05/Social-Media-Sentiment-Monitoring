FROM apache/airflow:2.5.0

# Ensure we run as the airflow user
USER airflow

# Install required Python packages as the airflow user
RUN pip install --no-cache-dir \
    textblob \
    pandas \
    requests \
    mysql-connector-python

# Download TextBlob corpora (optional but recommended for sentiment analysis)
RUN python -m textblob.download_corpora
