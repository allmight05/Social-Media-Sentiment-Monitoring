import os
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from textblob import TextBlob
from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.email import EmailOperator
from airflow.providers.mysql.hooks.mysql import MySqlHook
from sqlalchemy import create_engine

default_args = {
    'owner': 'airflow',
    'start_date': datetime(2025, 3, 19),
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

dag = DAG(
    'tech_sentiment_pipeline',
    default_args=default_args,
    schedule_interval='*/10 * * * *',  # Every 10 minutes
    catchup=False
)

# Load environment variables
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
# SMTP_USER = os.getenv("SMTP_USER")
# SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL = os.getenv("ALERT_EMAIL")
THRESHOLD = float(os.getenv("THRESHOLD", -0.3))


def extract_news_api_data():
    """Extracts news data from News API for the past week."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(weeks=1)
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': 'AI OR "artificial intelligence" OR "machine learning"',
        'language': 'en',
        'sortBy': 'publishedAt',
        'from': week_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
        'to': now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        'pageSize': 100,
        'apiKey': NEWS_API_KEY
    }
    response = requests.get(url, params=params)
    news_data = []
    if response.status_code == 200:
        articles = response.json().get("articles", [])
        for article in articles:
            title = article.get("title", "")
            if title:
                sentiment = TextBlob(title).sentiment.polarity
                news_data.append({
                    "source": "newsapi",
                    "title": title,
                    "sentiment": sentiment,
                    "timestamp": article.get("publishedAt"),
                    "company": article.get("source", {}).get("name")
                })
        print(f"News API: Fetched {len(news_data)} articles.")
    else:
        raise Exception(f"News API error: {response.status_code}")
    return news_data


def extract_hacker_news_data():
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(weeks=1)
    now_ts = int(now.timestamp())
    week_ago_ts = int(week_ago.timestamp())
    
    url = "http://hn.algolia.com/api/v1/search_by_date"
    params = {
        "tags": "story",
        "numericFilters": f"created_at_i>{week_ago_ts},created_at_i<{now_ts}",
        "hitsPerPage": 100,
    }
    response = requests.get(url, params=params)
    hn_data = []
    if response.status_code == 200:
        hits = response.json().get("hits", [])
        for hit in hits:
            title = hit.get("title")
            if title:
                sentiment = TextBlob(title).sentiment.polarity
                hn_data.append({
                    "source": "hackernews",
                    "title": title,
                    "sentiment": sentiment,
                    "timestamp": hit.get("created_at"),
                    "company": None 
                })
        print(f"Hacker News: Fetched {len(hn_data)} stories.")
    else:
        raise Exception(f"Hacker News API error: {response.status_code}")
    return hn_data


def extract_transform_load_news():
    mysql_hook = MySqlHook(mysql_conn_id='mysql_default')
    engine = mysql_hook.get_sqlalchemy_engine()

    news_data = extract_news_api_data()
    hn_data = extract_hacker_news_data()
    combined_data = news_data + hn_data

    if combined_data:
        df = pd.DataFrame(combined_data)
        df.to_sql('sentiment_data', engine, if_exists='append', index=False)
        print(f"Inserted {len(df)} records into sentiment_data table.")
    else:
        print("No data found to insert.")


def check_negative_sentiment(**context):
    mysql_hook = MySqlHook(mysql_conn_id='mysql_default')
    engine = mysql_hook.get_sqlalchemy_engine()
    
    query = """
    SELECT 
        AVG(sentiment) AS avg_sentiment,
        company
    FROM sentiment_data
    WHERE 
        timestamp >= NOW() - INTERVAL 7 DAY
    GROUP BY company
    HAVING COUNT(*) >= 5;
    """
    results = engine.execute(query).fetchall()
    avg_sentiments = {row["company"]: row["avg_sentiment"] for row in results if row["company"] is not None}
    context['ti'].xcom_push(key='avg_sentiments', value=avg_sentiments)
    
    if any(avg < THRESHOLD for avg in avg_sentiments.values()):
        return "send_alert"
    else:
        return "no_alert"



# Define tasks
load_news = PythonOperator(
    task_id='load_news',
    python_callable=extract_transform_load_news,
    dag=dag,
)

check_sentiment = BranchPythonOperator(
    task_id='check_sentiment',
    python_callable=check_negative_sentiment,
    provide_context=True,
    dag=dag,
)

# Define the HTML email template as a plain string
html_template = """
<h3>⚠️ Negative Sentiment Alert: Tech Sector</h3>
<p>Negative sentiment detected for:</p>
<ul>
    {% for company, avg in ti.xcom_pull(task_ids='check_sentiment', key='avg_sentiments').items() %}
        {% if avg < params.threshold %}
            <li><strong>{{ company }}</strong>: {{ "%.2f"|format(avg) }}</li>
        {% endif %}
    {% endfor %}
</ul>
<p>Threshold: {{ params.threshold }}</p>
"""

send_alert = EmailOperator(
    task_id='send_alert',
    conn_id="smtp_default",  # Add this line to use Airflow's SMTP connection
    to=ALERT_EMAIL,
    subject='⚠️ Negative Sentiment Alert: Tech Sector',
    html_content=html_template,
    params={"threshold": THRESHOLD},
    dag=dag,
)

no_alert = DummyOperator(
    task_id='no_alert',
    dag=dag,
)

# Set up task dependencies
load_news >> check_sentiment
check_sentiment >> [send_alert, no_alert]
