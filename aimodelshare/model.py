import os
import boto3
import json
import onnx
import numpy as np
import pandas as pd
import requests 
import json 

from datetime import datetime

from aimodelshare.aws import run_function_on_lambda, get_token, get_aws_token, get_aws_client

from aimodelshare.aimsonnx import _get_leaderboard_data,inspect_model_lambda


def _get_file_list(client, bucket, model_id):
    #  Reading file list {{{
    try:
        objects = client["client"].list_objects(Bucket=bucket, Prefix=model_id + "/")
    except Exception as err:
        return None, err

    file_list = []
    if "Contents" in objects:
        for key in objects["Contents"]:
            file_list.append(key["Key"].split("/")[1])
    #  }}}

    return file_list, None


def _delete_s3_object(client, bucket, model_id, filename):
    deletionobject = client["resource"].Object(bucket, model_id + "/" + filename)
    deletionobject.delete()

def _get_predictionmodel_key(unique_model_id,file_extension):
    if file_extension==".pkl":
        file_key = unique_model_id + "/runtime_model" + file_extension
        versionfile_key = unique_model_id + "/predictionmodel_1" + file_extension
    else:
        file_key = unique_model_id + "/runtime_model" + file_extension
        versionfile_key = unique_model_id + "/predictionmodel_1" + file_extension
    return file_key,versionfile_key


def _upload_onnx_model(modelpath, client, bucket, model_id, model_version):
    # Check the model {{{
    if not os.path.exists(modelpath):
        raise FileNotFoundError(f"The model file at {modelpath} does not exist")

    file_name = os.path.basename(modelpath)
    file_name, file_ext = os.path.splitext(file_name)

    assert (
        file_ext == ".onnx"
    ), "modelshareai api only supports .onnx models at the moment"
    # }}}

    # Upload the model {{{
    try:
        client["client"].upload_file(
            modelpath, bucket, model_id + "/onnx_model_mostrecent.onnx"
        )
        client["client"].upload_file(
            modelpath,
            bucket,
            model_id + "/onnx_model_v" + str(model_version) + file_ext,
        )
    except Exception as err:
        return err
    # }}}

def _upload_preprocessor(preprocessor, client, bucket, model_id, model_version):

  try:

    
    # Check the preprocessor {{{
    if not os.path.exists(preprocessor):
        raise FileNotFoundError(
            f"The preprocessor file at {preprocessor} does not exist"
        )

    
    file_name = os.path.basename(preprocessor)
    file_name, file_ext = os.path.splitext(file_name)
    
    from zipfile import ZipFile
    dir_zip = preprocessor

    #zipObj = ZipFile(os.path.join("./preprocessor.zip"), 'a')
    #/Users/aishwarya/Downloads/aimodelshare-master
    client["client"].upload_file(dir_zip, bucket, model_id + "/preprocessor_v" + str(model_version)+ ".zip")
  except Exception as e:
    print(e)


def _update_leaderboard(
    modelpath, eval_metrics, client, bucket, model_id, model_version
):
    # Loading the model and its metadata {{{
    if not os.path.exists(modelpath):
        raise FileNotFoundError(f"The model file at {modelpath} does not exist")

    model = onnx.load(modelpath)
    metadata = _get_leaderboard_data(model, eval_metrics)
    # }}}

    # Adding extra details to metadata {{{
    metadata["username"] = os.environ.get("username")
    metadata["timestamp"] = str(datetime.now())
    metadata["version"] = model_version
    # }}}
    
    #TODO: send above data in post call to /eval and update master table on back end rather than downloading locally.
    #Either way something is breaking and the s3 version should still work right??
    # Read existing table {{{
    try:
        leaderboard = client["client"].get_object(
            Bucket=bucket, Key=model_id + "/model_eval_data_mastertable.csv"



        )
        leaderboard = pd.read_csv(leaderboard["Body"], sep="\t")
        columns = leaderboard.columns

    except client["client"].exceptions.NoSuchKey:
        # Create leaderboard if not exists
        # FIXME: Find a better way to get columns
        columns = list(metadata.keys())
        leaderboard = pd.DataFrame(columns=columns)

    except Exception as err:
        raise err
    # }}}

    # Update the leaderboard {{{
    metadata = {col: metadata.get(col, None) for col in columns}
    leaderboard = leaderboard.append(metadata, ignore_index=True, sort=False)

    leaderboard_csv = leaderboard.to_csv(index=False, sep="\t")
    metadata.pop("model_config", "pop worked")

    try:
        s3_object = client["resource"].Object(
            bucket, model_id + "/model_eval_data_mastertable.csv"
        )
        s3_object.put(Body=leaderboard_csv)
        return metadata
    except Exception as err:
        return err
    # }}}

def submit_model(
    modelpath,
    apiurl,
    prediction_submission=None,
    preprocessor=None,
    sample_data=None,
):
    """
    Submits model/preprocessor to machine learning competition using live prediction API url generated by AI Modelshare library
    The submitted model gets evaluated and compared with all existing models and a leaderboard can be generated 
    ---------------
    Parameters:
    modelpath:  string ends with '.onnx'
                value - Absolute path to model file [REQUIRED] to be set by the user
                .onnx is the only accepted model file extension
                "example_model.onnx" filename for file in directory.
                "/User/xyz/model/example_model.onnx" absolute path to model file from local directory
    apiurl :    string 
                value - url to the live prediction REST API generated for the user's model 
                "https://example.execute-api.us-east-1.amazonaws.com/prod/m"
    prediction_submission:   one hot encoded y_pred
                    value - predictions for test data
                    [REQUIRED] for evaluation metriicts of the submitted model
    preprocessor:   string,default=None
                    value - absolute path to preprocessor file 
                    [REQUIRED] to be set by the user
                    "./preprocessor.zip" 
                    searches for an exported zip preprocessor file in the current directory
                    file is generated from preprocessor module using export_preprocessor function from the AI Modelshare library 
    sample_data:
    -----------------
    Returns
    response:   Model version if the model is submitted sucessfully
                error  if there is any error while submitting models
    
    """
    # Confirm that creds are loaded, print warning if not
    if all(["AWS_ACCESS_KEY_ID" in os.environ, 
            "AWS_SECRET_ACCESS_KEY" in os.environ,
            "AWS_REGION" in os.environ,
           "username" in os.environ, 
           "password" in os.environ]):
        pass
    else:
        return print("'Submit Model' unsuccessful. Please provide credentials with set_credentials().")
    
    aws_client=get_aws_client(aws_key=os.environ.get('AWS_ACCESS_KEY_ID'), 
                              aws_secret=os.environ.get('AWS_SECRET_ACCESS_KEY'), 
                              aws_region=os.environ.get('AWS_REGION'))
    apiurl=apiurl.replace('"','')

    # Get bucket and model_id for user {{{
    response, error = run_function_on_lambda(
        apiurl, **{"delete": "FALSE", "versionupdateget": "TRUE"}
    )
    if error is not None:
        raise error

    _, bucket, model_id = json.loads(response.content.decode("utf-8"))
    # }}}

    # Get file list for current bucket {{{
    model_files, err = _get_file_list(aws_client, bucket, model_id)
    if err is not None:
        raise err
    # }}}

    # Check if competition has been created, return warning if not {{{
    if "ytest.pkl" in model_files:
        pass
    else:
        no_competition_warning = ("Error: A model competition has not been created for this prediction API yet.\n\n"
                                  "Please use the create_competition function to upload ytest data for model evaluation\n" 
                                  " or update runtime model with the update_runtime_model function.")
        return print(no_competition_warning)
    # }}}

    # Delete recent model and/or preprocessor {{{
    recent_models = filter(lambda f: "mostrecent" in f, model_files)
    for model in recent_models:
        _delete_s3_object(aws_client, bucket, model_id, model)

    model_files = list(filter(lambda f: "mostrecent" not in f, model_files))
    # }}}

    # Get new model version {{{
    model_versions = [os.path.splitext(f)[0].split("_")[-1][1:] for f in model_files]

    model_versions = filter(lambda v: v.isnumeric(), model_versions)
    model_versions = list(map(int, model_versions))

    if model_versions:
        model_version = max(model_versions) + 1
    else:
        model_version = 1
    # }}}

    # Upload the preprocessor {{{
    if preprocessor is not None:
        err = _upload_preprocessor(
            preprocessor, aws_client, bucket, model_id, model_version
        )
        if err is not None:
            raise err
    # }}}

    # Upload the model {{{
    err = _upload_onnx_model(modelpath, aws_client, bucket, model_id, model_version)
    if err is not None:
        raise err
    # }}}

    if prediction_submission is not None:
        if type(prediction_submission) is not list:
            prediction_submission=prediction_submission.tolist()
        else: 
            pass

        if all(isinstance(x, (np.int64)) for x in prediction_submission):
              prediction_submission = [int(i) for i in prediction_submission]
        else: 
            pass


    
    post_dict = {"y_pred": prediction_submission,
           "return_eval": "True",
           "return_y": "False"}
 
    headers = { 'Content-Type':'application/json', 'authorizationToken': os.environ.get("AWS_TOKEN"), } 
    apiurl_eval=apiurl[:-1]+"eval"
    prediction = requests.post(apiurl_eval,headers=headers,data=json.dumps(post_dict)) 

    eval_metrics=json.loads(prediction.text)


    if all(value == None for value in eval_metrics.values()):
        print("Failed to calculate evaluation metrics. Please check the format of the submitted predictions.")




    # Upload model metrics and metadata {{{
    modelleaderboarddata = _update_leaderboard(
        modelpath, eval_metrics, aws_client, bucket, model_id, model_version
    )

    def dict_clean(items):
      result = {}
      for key, value in items:
          if value is None:
              value = '0'
          result[key] = value
      return result

    if isinstance(modelleaderboarddata, Exception):
      raise err
    else:
      dict_str = json.dumps(modelleaderboarddata)
    #convert None type values to string
      modelleaderboarddata_cleaned = json.loads(dict_str, object_pairs_hook=dict_clean)

    # Update model version and sample data {{{
    #data_types = None
    #data_columns = None
    #if sample_data is not None and isinstance(sample_data, pd.DataFrame):
    #    data_types = list(sample_data.dtypes.values.astype(str))
    #    data_columns = list(sample_data.columns)

    #kwargs = {
    #    "delete": "FALSE",
    #    "versionupdateget": "FALSE",
    #    "versionupdateput": "TRUE",
    #    "version": model_version,
    #    "input_feature_dtypes": data_types,
    #    "input_feature_names": data_columns,
    #}
    #response, error = run_function_on_lambda(apiurl, aws_token, **kwargs)
    #if error is not None:
    #    raise error
    # }}}
    modelsubmissiontags=input("Insert search tags to help users find your model (optional): ")
    modelsubmissiondescription=input("Provide any useful notes about your model (optional): ")
    aimsurl=input("Share code: Insert AI Model Share url to Jupyter notebook with model code(optional): ")
    githuburl=input("Share code: Insert Github url to Jupyter notebook with model code(optional): ")

    #Update competition data
    bodydata = {"apiurl": apiurl,
                "submissions": model_version,
                 "contributoruniquenames":os.environ.get('username'),
                }
    
    # Get the response
    headers_with_authentication = {'Content-Type': 'application/json', 'authorizationToken': os.environ.get("AWS_TOKEN"), 'Access-Control-Allow-Headers':
                                   'Content-Type,X-Amz-Date,authorizationToken,Access-Control-Allow-Origin,X-Api-Key,X-Amz-Security-Token,Authorization', 'Access-Control-Allow-Origin': '*'}
    # competitiondata lambda function invoked through below url to update model submissions and contributors
    requests.post("https://o35jwfakca.execute-api.us-east-1.amazonaws.com/dev/modeldata",
                  json=bodydata, headers=headers_with_authentication)
    
    #Update model architecture data
    bodydatamodels = {
                "apiurl": apiurl,
                "modelsummary":json.dumps(inspect_model(apiurl,int(model_version)).to_json()),
                "Private":"FALSE",
                "modelsubmissiondescription": modelsubmissiondescription,
                "modelsubmissiontags":modelsubmissiontags ,

                "githubipynburl":githuburl,
                "aimsipynburl": aimsurl}

    bodydatamodels.update(modelleaderboarddata_cleaned)
    d = bodydatamodels


    keys_values = d.items()


    bodydatamodels_allstrings = {str(key): str(value) for key, value in keys_values}



    # Get the response
    headers_with_authentication = {'Content-Type': 'application/json', 'authorizationToken': os.environ.get("AWS_TOKEN"), 'Access-Control-Allow-Headers':
                                   'Content-Type,X-Amz-Date,authorizationToken,Access-Control-Allow-Origin,X-Api-Key,X-Amz-Security-Token,Authorization', 'Access-Control-Allow-Origin': '*'}
    # competitiondata lambda function invoked through below url to update model submissions and contributors
    response=requests.post("https://eeqq8zuo9j.execute-api.us-east-1.amazonaws.com/dev/modeldata",
                  json=bodydatamodels_allstrings, headers=headers_with_authentication)
    return "Your model has been submitted as model version "+str(model_version)

  
def update_runtime_model(apiurl, model_version=None):
    """
    apiurl: string of API URL that the user wishes to edit
    new_model_version: string of model version number (from leaderboard) to replace original model 
    """
    # Confirm that creds are loaded, print warning if not
    if all(["AWS_ACCESS_KEY_ID" in os.environ, 
            "AWS_SECRET_ACCESS_KEY" in os.environ,
            "AWS_REGION" in os.environ,
           "username" in os.environ, 
           "password" in os.environ]):
        pass
    else:
        return print("'Update Runtime Model' unsuccessful. Please provide credentials with set_credentials().")

    # Create user session
    aws_client=get_aws_client(aws_key=os.environ.get('AWS_ACCESS_KEY_ID'), 
                              aws_secret=os.environ.get('AWS_SECRET_ACCESS_KEY'), 
                              aws_region=os.environ.get('AWS_REGION'))
    
    user_sess = boto3.session.Session(aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'), 
                                      aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'), 
                                      region_name=os.environ.get('AWS_REGION'))
    
    s3 = user_sess.resource('s3')
    model_version=str(model_version)
    # Get bucket and model_id for user based on apiurl {{{
    response, error = run_function_on_lambda(
        apiurl, **{"delete": "FALSE", "versionupdateget": "TRUE"}
    )
    if error is not None:
        raise error

    _, api_bucket, model_id = json.loads(response.content.decode("utf-8"))
    # }}}

    try:
        leaderboard = aws_client["client"].get_object(
            Bucket=api_bucket, Key=model_id + "/model_eval_data_mastertable.csv"



        )
        leaderboard = pd.read_csv(leaderboard["Body"], sep="\t")
        columns = leaderboard.columns
        metric_names=["accuracy","f1_score","precision","recall","r2","mse","mae"]
        leaderboardversion=leaderboard[leaderboard['version']==int(model_version)]
        leaderboardversion=leaderboardversion.dropna(axis=1)
        metric_names_subset=list(set(metric_names).intersection(leaderboardversion.columns))
        leaderboardversiondict=leaderboardversion.loc[:,metric_names_subset].to_dict('records')[0]
    except Exception as err:
        raise err

    # Get file list for current bucket {{{
    model_files, err = _get_file_list(aws_client, api_bucket, model_id)
    if err is not None:
        raise err
    # }}}

    # extract subfolder objects specific to the model id
    folder = s3.meta.client.list_objects(Bucket=api_bucket, Prefix=model_id+"/")
    bucket = s3.Bucket(api_bucket)
    file_list = [file['Key'] for file in folder['Contents']]
    s3 = boto3.resource('s3')
    model_source_key = model_id+"/onnx_model_v"+str(model_version)+".onnx"
    preprocesor_source_key = model_id+"/preprocessor_v"+str(model_version)+".zip"
    model_copy_source = {
          'Bucket': api_bucket,
          'Key': model_source_key
        }
    preprocessor_copy_source = {
          'Bucket': api_bucket,
          'Key': preprocesor_source_key
      }
    # Sending correct model metrics to front end 
    bodydatamodelmetrics={"apiurl":apiurl,
                          "versionupdateput":"TRUE",
                          "verified_metrics":"TRUE",
                          "eval_metrics":json.dumps(leaderboardversiondict)}
 
    headers = { 'Content-Type':'application/json', 'authorizationToken': os.environ.get("AWS_TOKEN"), } 
    prediction = requests.post("https://bhrdesksak.execute-api.us-east-1.amazonaws.com/dev/modeldata",headers=headers,data=json.dumps(bodydatamodelmetrics)) 

    # overwrite runtime_model.onnx file & runtime_preprocessor.zip files: 
    if (model_source_key in file_list) & (preprocesor_source_key in file_list):
        response = bucket.copy(model_copy_source, model_id+"/"+'runtime_model.onnx')
        response = bucket.copy(preprocessor_copy_source, model_id+"/"+'runtime_preprocessor.zip')
        return print('Runtime model & preprocessor for api: '+apiurl+" updated to model version "+model_version+".\n\nModel metrics are now updated and verified for this model playground.")
    else:
        # the file resource to be the new runtime_model is not available
        return 'New Runtime Model version ' + model_version + ' not found.'
    

def _extract_model_metadata(model, eval_metrics=None):
    # Getting the model metadata {{{
    graph = model.graph

    if eval_metrics is not None:
        metadata = eval_metrics
    else:
        metadata = dict()

    metadata["num_nodes"] = len(graph.node)
    metadata["depth_test"] = len(graph.initializer)
    metadata["num_params"] = sum(np.product(node.dims) for node in graph.initializer)

    # layers = ""
    # for node in graph.node:
    #     # consider type and get node attributes (??)
    #     layers += (
    #         node.op_type
    #         + "x".join(str(d.ints) for d in node.attribute if hasattr(d, 'ints'))
    #     )
    metadata["layers"] = "; ".join(node.op_type for node in graph.node)

    inputs = ""
    for inp in graph.input:
        dims = []
        for d in inp.type.tensor_type.shape.dim:
            if d.dim_param != "":
                dims.append(d.dim_param)
            else:
                dims.append(str(d.dim_value))

        metadata["input_shape"] = dims
        inputs += f"{inp.name} ({'x'.join(dims)})"
    metadata["inputs"] = inputs

    outputs = ""
    for out in graph.output:
        dims = []
        for d in out.type.tensor_type.shape.dim:
            if d.dim_param != "":
                dims.append(d.dim_param)
            else:
                dims.append(str(d.dim_value))

        outputs += f"{out.name} ({'x'.join(dims)})"
    metadata["outputs"] = outputs
    # }}}

    return metadata

__all__ = [
    submit_model,
    _extract_model_metadata,
    update_runtime_model
]
