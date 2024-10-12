import openai
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse
from pymilvus import Collection, FieldSchema, CollectionSchema, DataType, connections
import aiofiles
import os
import fitz  # PyMuPDF
import numpy as np
from enum import Enum
from typing import Optional

from openai import OpenAI
import os
from dotenv import load_dotenv

# Initialize FastAPI app
app = FastAPI()

# OpenAI API key

load_dotenv()
client = OpenAI()



badlands = MilvusClient("./milvus_open_context_v0.db")

badlands.create_collection(
    collection_name="open_context_v0",
    dimension=3072  # The vectors we will use in this demo has 384 dimensions
)
# Connect to MilvusDB
#connections.connect(alias="default", host='localhost', port='19530')

# Define Milvus schema (create if it doesn't exist)
fields = [
    FieldSchema(name="client_id", dtype=DataType.INT64),
    FieldSchema(name="course_id", dtype=DataType.STRING),  # course_id as STRING
    FieldSchema(name="lecture_id", dtype=DataType.STRING),  # lecture_id as STRING
    FieldSchema(name="time_stamp", dtype=DataType.STRING),
    FieldSchema(name="data_source", dtype=DataType.STRING),  # New field for data source
    FieldSchema(name="chunk_number", dtype=DataType.INT64),  # Field to track chunk number
    FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=3072),  # Embedding dimensions
    FieldSchema(name="text_chunk", dtype=DataType.STRING)  # Store actual text chunk
]

schema = CollectionSchema(fields, description="Embeddings and metadata collection")
collection = Collection(name="data_embeddings", schema=schema)


# Enum for data source types
class DataSource(str, Enum):
    audio = "audio"
    pdf = "pdf"
    typed_notes = "typed-notes"


# Helper function to process audio with Whisper and OpenAI Embeddings
async def process_audio(file_path: str):
    try:
        # 1. Transcribe the audio using Whisper API
        audio_file = open(file_path, "rb")
        transcript = openai.Audio.transcribe("whisper-1", audio_file)
        text = transcript["text"]

        # 2. Generate embeddings using OpenAI's embedding API
        response = openai.Embedding.create(
            input=text,
            model="text-embedding-3-large"
        )
        embedding = response['data'][0]['embedding']  # This will be a 3072-dimensional vector

        return text, embedding
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Helper function to process PDF and chunk text by paragraph
async def process_pdf(file_path: str):
    try:
        doc = fitz.open(file_path)
        text_chunks = []

        for page in doc:
            text = page.get_text("text")
            paragraphs = text.split("\n\n")  # Split text by paragraphs (two newlines)
            text_chunks.extend([para.strip() for para in paragraphs if para.strip()])

        # Vectorize each chunk and store it with metadata
        embeddings = []
        chunk_number = 1
        for chunk in text_chunks:
            response = openai.Embedding.create(
                input=chunk,
                model="text-embedding-3-large"
            )
            embedding = response['data'][0]['embedding']
            embeddings.append((chunk_number, embedding, chunk))
            chunk_number += 1

        return embeddings, text_chunks  # Return embeddings and original text chunks

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Helper function to process typed text and chunk it by new lines
async def process_typed_text(text: str):
    try:
        text_chunks = text.split("\n")  # Split text by new lines

        # Vectorize each chunk and store it with metadata
        embeddings = []
        chunk_number = 1
        for chunk in text_chunks:
            if chunk.strip():  # Skip empty lines
                response = openai.Embedding.create(
                    input=chunk,
                    model="text-embedding-3-large"
                )
                embedding = response['data'][0]['embedding']
                embeddings.append((chunk_number, embedding, chunk))
                chunk_number += 1

        return embeddings, text_chunks  # Return embeddings and original text chunks

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Helper function to save files
async def save_file(client_id: int, course_id: str, lecture_id: str, file: UploadFile, file_type: str) -> str:
    directory = f"uploaded_files/{client_id}/{course_id}/{lecture_id}/{file_type}/"
    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, file.filename)

    async with aiofiles.open(file_path, 'wb') as out_file:
        content = await file.read()
        await out_file.write(content)

    return file_path


@app.post("/upload_data/")
async def upload_data(
    client_id: int,
    course_id: str,  # course_id as string
    lecture_id: str,  # lecture_id as string
    time_stamp: str,
    data_source: DataSource,
    file: Optional[UploadFile] = File(None),
    typed_text: Optional[str] = Form(None)
):
    try:
        if data_source == DataSource.audio:
            if not file:
                raise HTTPException(status_code=400, detail="Audio file is required for 'audio' data source.")

            # Save the audio file
            file_path = await save_file(client_id, course_id, lecture_id, file, "audio")

            # Process audio (transcription and embedding)
            text, embedding = await process_audio(file_path)

            # Store embedding and metadata in MilvusDB
            data_to_insert = [
                [client_id],  # Client ID
                [course_id],  # Course ID
                [lecture_id],  # Lecture ID
                [time_stamp],  # Time stamp
                [data_source],  # Data source
                [1],  # Chunk number (for audio, it's just 1)
                [embedding],  # Embedding vector
                [text]  # Store the transcription text
            ]
            collection.insert(data_to_insert)

        elif data_source == DataSource.pdf:
            if not file:
                raise HTTPException(status_code=400, detail="PDF file is required for 'pdf' data source.")

            # Save the PDF file
            file_path = await save_file(client_id, course_id, lecture_id, file, "pdf")

            # Process the PDF, chunk text, and generate embeddings
            embeddings, text_chunks = await process_pdf(file_path)

            # Store each chunk and metadata in MilvusDB
            for chunk_number, embedding, chunk in embeddings:
                data_to_insert = [
                    [client_id],  # Client ID
                    [course_id],  # Course ID
                    [lecture_id],  # Lecture ID
                    [time_stamp],  # Time stamp
                    [data_source],  # Data source
                    [chunk_number],  # Chunk number
                    [embedding],  # Embedding vector
                    [chunk]  # Store the text chunk
                ]
                collection.insert(data_to_insert)

        elif data_source == DataSource.typed_notes:
            if not typed_text:
                raise HTTPException(status_code=400, detail="Typed text is required for 'typed_notes' data source.")

            # Save typed notes as a file
            directory = f"uploaded_files/{client_id}/{course_id}/{lecture_id}/typed_notes/"
            os.makedirs(directory, exist_ok=True)
            file_path = os.path.join(directory, f"typed_notes_{lecture_id}.txt")
            async with aiofiles.open(file_path, 'w') as f:
                await f.write(typed_text)

            # Process the typed text, chunk it, and generate embeddings
            embeddings, text_chunks = await process_typed_text(typed_text)

            # Store each chunk and metadata in MilvusDB
            for chunk_number, embedding, chunk in embeddings:
                data_to_insert = [
                    [client_id],  # Client ID
                    [course_id],  # Course ID
                    [lecture_id],  # Lecture ID
                    [time_stamp],  # Time stamp
                    [data_source],  # Data source
                    [chunk_number],  # Chunk number
                    [embedding],  # Embedding vector
                    [chunk]  # Store the text chunk
                ]
                collection.insert(data_to_insert)

        return {
            "message": "Data processed successfully!",
            "data_source": data_source,
            "embedding_stored": True
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process data: {str(e)}")


@app.get("/files/{client_id}/{course_id}/{lecture_id}/{file_type}")
async def get_file(client_id: int, course_id: str, lecture_id: str, file_type: DataSource):
    """
    Endpoint to retrieve raw data files (audio, pdf, typed_text)
    """
    directory = f"uploaded_files/{client_id}/{course_id}/{lecture_id}/{file_type}/"
    if not os.path.exists(directory):
        raise HTTPException(status_code=404, detail="File not found")

    # Return the first file found in the directory
    files = os.listdir(directory)
    if not files:
        raise HTTPException(status_code=404, detail="No files found in the specified directory.")

    file_path = os.path.join(directory, files[0])
    return FileResponse(path=file_path, filename=files[0])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)