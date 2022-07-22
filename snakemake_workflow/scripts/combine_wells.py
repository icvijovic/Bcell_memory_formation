import sys

import argparse
import pandas as pd
import numpy as np

###########################################################################

parser = argparse.ArgumentParser()
parser.add_argument('input_paths', nargs='+', 
                    help="list of files to combine")
parser.add_argument('-name', required="True", 
                    help="name for combined sample set")
parser.add_argument('-samplesheet', nargs='+', required=True, 
                    help="samplesheet containing sample metadata")
parser.add_argument('-outdir', default='.')

args = parser.parse_args()

filenames = args.input_paths
path_to_samplesheet = args.samplesheet
outdir = args.outdir
name = args.name

samplesheet = pd.concat([pd.read_table(
    "{}".format(x), dtype='str')
    for x in path_to_samplesheet
], ignore_index=True)

samplesheet.well = name + "_" + samplesheet.well
samplesheet.set_index('well', inplace=True)

metadata_columns = samplesheet.columns.values

all_data = []

for filename in filenames:
    sample_index = filename.split("/")[-1].split("_igblast.tsv")[0]
    sample_metadata = samplesheet.loc[sample_index]

    try:
        sample_df=pd.read_table(filename)
        for metadata_col in metadata_columns:
            sample_df[metadata_col] = sample_metadata[metadata_col]
        sample_df['sample_index'] = sample_index
        sample_df.sequence_id = "SAMPLE_INDEX="+sample_index + "|" + sample_df.sequence_id
        all_data.append(sample_df)
    except pd.errors.EmptyDataError:
        pass

print("concatenating...")
df = pd.concat(all_data, ignore_index = True)
print('saving...')
df.to_csv("{path}/{file_id}_igblast.tsv.gz".format(path=outdir, file_id=name), sep='\t', index=False)

