**Current objective**
POINT BEDROCK KNOWLEDGEBASE TO OUR S3 bucket, do some testing, then clean the data. 

Terraform for: API gateway /health
               Lamda for GET /health
               Cloudwatch log group
               Budget alarms
               DynamoDB table
               SQS queue + DLQ 



Data source - Bimmerpost
              Youtube
              B58 facebook groups
              NHTSA TSB
              LLMs with links ot service manuals/ other tech docs.
              

Front end - **Collects: VIN or Year/Make/Model, symptoms, answers to follow-up questions**
            x.js
            Color pallete - Black, White, Blue, Yellow.
            Mechanic Bee logo.
            design will come later
            Links/citations to the sources used
            Use the NHTSA vPIC Vehicle API to decode VIN and fetch vehicle attributes. - ask for VIN (optional) → decode it → fill make/model/year/engine/trim automatically
            Structures intake form - CEL, no start/hard start, Rough idle, misfire, overheating, ac not cooling, trans slipping, battery drain, x noises.
            Ask for context - When did it start, did it get worse over time, temp, mileage, dash lights, OBD2 codes if possible
            

Backend - **VIN decode, data normalization, search through data layer, ranking algo, response assembly**
          FastAPI
          Decision tree + AI combo
          Vehicle + symptom pattern -> output likely causes based on real experience. 
          MVP ^
          Extend out to log analysis, based on experience and baseline docs based on vehicle + setup.

LLM usage - **Used just for structuring text, and %Humanization% summarize findings + citation**
            OpenAI local: pay for token

Infra - Serverless 
        Docs for ECS/RDS evolution
        [ Browser ]
             |
       [ CloudFront ]
             |
      [ API Gateway (HTTP API) ]
             |
      [ Lambda (API handlers) ]
             |
       [ DynamoDB ]
            |
         [ SQS ] <--> [ Lambda (Worker) ]
            |
       [ DynamoDB ]
Architecture: Serverless MVP
API: API Gateway (HTTP API) + Lambda (Python)
State: DynamoDB
Async: SQS + Lambda worker
Frontend: S3 + CloudFront
IaC: Terraform 
Observability: CloudWatch
Budget: $30/month, alarm at $20













Explain this data model: **3) Data model (simple but sufficient)

Tables

sessions(id, created_at, user_agent, ...)

vehicles(id, vin, year, make, model, trim, engine, metadata_json)

intakes(id, session_id, vehicle_id, primary_symptom, raw_text, structured_json)

answers(id, intake_id, question_id, answer_value, raw_text)

diagnoses(id, intake_id, candidate_json, created_at)

documents(id, source, title, url, vehicle_tags_json, text, embedding_vector)

question_bank(id, domain, question_text, answer_type, logic_json)**
